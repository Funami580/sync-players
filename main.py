#!/usr/bin/env python3

# Options to modify the behavior of the program
SYNC_STOP = False
SYNC_PAUSE_ON_END_REACHED = False

# Here starts the main program
import time
import gi
gi.require_version('Playerctl', '2.0')
from gi.repository import Playerctl, GLib
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable

COLUMN_SYNC_KEY = None
COLUMN_APP_KEY = None
COLUMN_STATUS_KEY = None
COLUMN_TITLE_KEY = None
ROW_ID_TO_PLAYER = {}
PLAYER_TO_SYNCHED = {}
PLAYERCTL_MAIN_LOOP = None

def sync_text(synced: bool) -> str:
    return "No" if not synced else Text("Yes", style="white on green")

class TableApp(App):
    def compose(self) -> ComposeResult:
        yield DataTable()

    def on_mount(self) -> None:
        global COLUMN_SYNC_KEY, COLUMN_APP_KEY, COLUMN_STATUS_KEY, COLUMN_TITLE_KEY, PLAYERCTL_WORKER
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        COLUMN_SYNC_KEY, COLUMN_APP_KEY, COLUMN_STATUS_KEY, COLUMN_TITLE_KEY = table.add_columns("Sync", "App", "Status", "Title")
        self.playerctl()

    def on_unmount(self) -> None:
        global PLAYERCTL_MAIN_LOOP
        if PLAYERCTL_MAIN_LOOP is not None:
            PLAYERCTL_MAIN_LOOP.quit()

    async def on_data_table_row_selected(self, row_key) -> None:
        row_key = row_key.row_key
        player = ROW_ID_TO_PLAYER[row_key]
        is_synched = PLAYER_TO_SYNCHED[player]
        PLAYER_TO_SYNCHED[player] = new_synched = not is_synched
        table = self.query_one(DataTable)
        table.update_cell(row_key, COLUMN_SYNC_KEY, sync_text(new_synched), update_width=True)

    @work(thread=True)
    async def playerctl(self) -> None:
        global PLAYERCTL_MAIN_LOOP
        table = self.query_one(DataTable)
        manager = Playerctl.PlayerManager()
        player_to_row_id = {}
        player_to_player_offset = {}
        last_seek_event = None

        def status_text(status: Playerctl.PlaybackStatus) -> str:
            if status == Playerctl.PlaybackStatus.PLAYING:
                return "Playing"
            elif status == Playerctl.PlaybackStatus.PAUSED:
                return "Paused"
            elif status == Playerctl.PlaybackStatus.STOPPED:
                return "Stopped"
            else:
                return "Unknown"

        def on_status(player, status):
            global SYNC_STOP, SYNC_PAUSE_ON_END_REACHED
            row_id = player_to_row_id[player]

            if status == Playerctl.PlaybackStatus.PAUSED and not SYNC_PAUSE_ON_END_REACHED and "mpris:length" in player.props.metadata.keys():
                if abs(player.props.metadata["mpris:length"] - player.props.position) < 500000: # less than 0.5 seconds around end
                    self.call_from_thread(table.update_cell, row_id, COLUMN_STATUS_KEY, status_text(status), update_width=True)
                    return

            if PLAYER_TO_SYNCHED[player]:
                for other_player, is_synched in PLAYER_TO_SYNCHED.items():
                    if is_synched:
                        if player == other_player:
                            continue
                        if status == Playerctl.PlaybackStatus.PLAYING:
                            if other_player.props.can_play:
                                pos0 = player.props.position
                                pos1 = other_player.props.position
                                player_to_player_offset[(player, other_player)] = pos0 - pos1
                                player_to_player_offset[(other_player, player)] = pos1 - pos0
                                for other_player2, is_synched2 in PLAYER_TO_SYNCHED.items():
                                    if is_synched2:
                                        if other_player2 == other_player or other_player2 == player:
                                            continue
                                        pos2 = other_player2.props.position
                                        player_to_player_offset[(other_player, other_player2)] = pos1 - pos2
                                        player_to_player_offset[(other_player2, other_player)] = pos2 - pos1
                                other_player.play()
                        elif status == Playerctl.PlaybackStatus.PAUSED:
                            if other_player.props.can_pause:
                                other_player.pause()
                                player_to_player_offset.clear()
                        elif status == Playerctl.PlaybackStatus.STOPPED:
                            if SYNC_STOP:
                                other_player.stop()
                                player_to_player_offset.clear()

            self.call_from_thread(table.update_cell, row_id, COLUMN_STATUS_KEY, status_text(status), update_width=True)

        def on_metadata(player, metadata):
            row_id = player_to_row_id[player]
            self.call_from_thread(table.update_cell, row_id, COLUMN_TITLE_KEY, player.get_title(), update_width=True)

        def on_seek(player, position):
            # Position is absolute and in microseconds
            nonlocal last_seek_event
            current_seek_event = time.time()

            if last_seek_event is not None and (current_seek_event - last_seek_event) < 1:
                return

            last_seek_event = current_seek_event

            if PLAYER_TO_SYNCHED[player]:
                for other_player, is_synched in PLAYER_TO_SYNCHED.items():
                    if is_synched:
                        if player == other_player:
                            continue

                        old_absolute_other_position = other_player.props.position

                        try:
                            offset = player_to_player_offset[(player, other_player)]
                            new_absolute_other_position = position - offset
                            relative_forward = new_absolute_other_position - old_absolute_other_position
                            other_player.seek(relative_forward)
                        except KeyError:
                            pass

        def init_player(name):
            player = Playerctl.Player.new_from_name(name)
            player.connect('playback-status', on_status)
            player.connect('metadata', on_metadata)
            player.connect('seeked', on_seek)
            manager.manage_player(player)
            player_name = player.props.player_name
            player_status = player.props.playback_status
            player_title = player.get_title()
            row_id = self.call_from_thread(table.add_row, sync_text(False), player_name, status_text(player_status), player_title)
            player_to_row_id[player] = row_id
            ROW_ID_TO_PLAYER[row_id] = player
            PLAYER_TO_SYNCHED[player] = False

        def exit_player(player):
            row_id = player_to_row_id[player]
            self.call_from_thread(table.remove_row, row_id)
            for player1, player2 in list(player_to_player_offset.keys()):
                if player1 == player or player2 == player:
                    del player_to_player_offset[(player1, player2)]
            del PLAYER_TO_SYNCHED[player]
            del ROW_ID_TO_PLAYER[player]
            del player_to_row_id[player]

        def on_name_appeared(manager, name):
            init_player(name)

        def on_player_vanished(manager, player):
            exit_player(player)

        manager.connect('name-appeared', on_name_appeared)
        manager.connect('player-vanished', on_player_vanished)

        for name in manager.props.player_names:
            init_player(name)

        PLAYERCTL_MAIN_LOOP = main = GLib.MainLoop()
        main.run()

app = TableApp()
app.run()
