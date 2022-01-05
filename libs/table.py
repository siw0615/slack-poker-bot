import time
import random
import threading as thread
import uuid
from typing import List, Dict
import libs.game as lgame
import bots.game as bgame
from slackapi.payload import get_mentioned_string, build_payload, build_info_str, card_to_emoji, build_prompt_payload
from .poker_bot import PokerBot
from .player import Player
import logging
from .storage import Storage

MAX_AWAIT = 60
INITIAL_CHIPS = 1000
INITIAL_TABLE_CHIPS = 200

logger = logging.getLogger(__name__)


class Table:
    def __init__(self, owner: str, storage: Storage):
        self.uid = str(uuid.uuid4())
        self.game = lgame.Game(self.update_payload)
        self.owner = owner
        self.players: List[Player] = []
        self.players_user2pos: Dict[str, int] = dict()
        self.countdown = MAX_AWAIT
        self.exe_pos_local = -1
        self.round_status_local = ""
        self.msg_ts = ""
        self.btn = -1
        self.ante = 2
        self.counter = 0  # number of games
        self.timer_thread = None
        self.poker_bots: Dict[str, PokerBot] = {}
        self.storage = storage
        self.max_name_len = 0
        self.is_stall_payload = False

    def join(self, userid, username, is_bot: bool = False):
        """Join a table, return (pos, nplayers, total_chip, table_chip, err)"""
        chip, err = self.storage.fetch_user_chip(userid)
        if err is not None:
            self.storage.create_user(userid, INITIAL_CHIPS)
            chip = INITIAL_CHIPS

        if is_bot:
            self.storage.change_user_chip(userid, INITIAL_CHIPS)

        player = Player(userid, username, chip)
        pos = len(self.players)

        if userid in list(map(lambda p: p.userid, self.players)):
            pos = self.players_user2pos[userid]
            player = self.players[pos]
            if not player.is_leaving():
                return -1, -1, -1, -1, "already in this table"
            player.set_entering()

        if len(username) > self.max_name_len:
            self.max_name_len = len(username)

        player.chip, err = self.storage.transfer_user_chip_to_table(player.userid, INITIAL_TABLE_CHIPS, self.uid)
        if err is not None:
            return -1, -1, -1, -1, err
        if player.chip == 0:
            return -1, -1, -1, -1, "no money, fuck"

        if player not in self.players:
            self.players.append(player)
            self.players_user2pos[player.userid] = pos
        return pos, self.get_ready_player_num(), chip, player.chip, None

    def force_close(self):
        self.game.force_end()
        if self.timer_thread is not None:
            self.timer_thread.join()
        for player in self.players:
            self.leave(player.userid)

    def get_ready_player_num(self):
        return len(list(filter(lambda p: not p.is_leaving(), self.players)))

    def leave(self, userid):
        """Leave a table, return (nplayers, err)"""
        if userid not in list(map(lambda player: player.userid, self.players)):
            return -1, "is not in this table"
        player_pos = self.players_user2pos[userid]
        player = self.players[player_pos]
        self.storage.leave_table(player.userid, self.uid, player.chip)
        self.players[player_pos].set_leaving()
        return self.get_ready_player_num(), None

    def start(self, user_id):
        """Start a game, return (hands, err)"""
        if self.game.is_running():
            return None, "already running"

        self.players = list(filter(lambda p: not p.is_leaving(), self.players))
        if len(self.players) < 2:
            return None, "Failed to start, because this game requires at least TWO players"

        if self.btn == -1:
            self.btn = random.randint(0, len(self.players) - 1)
        else:
            self.btn = (self.btn + 1) % len(self.players)

        active_players = self.players.copy()
        for player in active_players:
            player.set_normal()
        self.update_user2pos()
        self.game.start(active_players, self.ante, self.btn)
        logger.debug("%s: game start successfully", self.uid)
        hands = []
        for pos, player in enumerate(active_players):
            hands.append({
                "id": player.userid,
                "hand": self.game.get_cards_by_pos(pos)
            })
        if self.timer_thread is not None:
            self.timer_thread.join()  # FIXME: necessary?
        self.timer_thread = thread.Thread(target=self.timer_function)
        self.timer_thread.start()
        return hands, None

    def update_user2pos(self):
        self.players_user2pos.clear()
        for pos, player in enumerate(self.players):
            self.players_user2pos[player.userid] = pos

    def add_bot_player(self):
        bot_id = f"bot_{len(self.poker_bots)}"
        pos, _, _, table_chips, err = self.join(bot_id, bot_id, True)
        if err is not None:
            return err
        self.poker_bots[bot_id] = PokerBot(self.uid)
        bgame.send_to_channel_by_table_id(self.uid, f"{bot_id} has joined at pos {pos} with ${table_chips}")
        return None

    def bot_function(self):
        game = self.game

        if game.get_round_status_name() == "END":
            logger.debug("bot_function: game end, exit")
            return

        exe_player = game.players[game.exe_pos]

        logger.debug("bot_function: pos: %d", game.exe_pos)
        logger.debug("bot_function: poker_bots: %s", str(self.poker_bots))

        if exe_player.userid in self.poker_bots:
            self.poker_bots[exe_player.userid].react(game, game.exe_pos)

    def timer_function(self):
        time.sleep(3)
        while True:
            logger.debug("%s: timer trigger", self.uid)
            start_time = time.time()
            should_stop = self.mainloop()
            if should_stop:
                logger.debug("%s: timer stop", self.uid)
                break
            self.bot_function()
            elapsed_time = time.time() - start_time
            if elapsed_time < 1.0:
                time.sleep(1.0 - elapsed_time)

    def mainloop(self):
        round_status = self.game.get_round_status_name()
        exe_pos = self.game.exe_pos

        logger.debug("%s: mainloop", self.uid)

        if round_status == "END":
            logger.debug("%s: mainloop exit", self.uid)
            bgame.send_to_channel_by_table_id(self.uid, "Game Over!")
            self.game.result.execute()
            self.update_chip()
            self.show_result(self.game.result)
            self.players = list(filter(lambda p: not p.is_leaving(), self.players))
            self.is_stall_payload = True
            self.msg_ts = ""
            return True

        if self.countdown == 0:
            logger.debug("%s: mainloop countdown", self.uid)
            player = self.players[exe_pos]
            player.timeout_count += 1
            if player.timeout_count >= 2:
                self.game.pfold(exe_pos)
                bgame.send_to_channel_by_table_id(
                    self.uid, f"timeout {player.timeout_count} times: {player.username} is leaving the table")
                player.set_leaving()
            else:
                if self.game.is_check_permitted(exe_pos):
                    self.game.pcheck(exe_pos)
                else:
                    self.game.pfold(exe_pos)
            self.countdown = MAX_AWAIT
            return False

        if self.round_status_local != round_status or exe_pos != self.exe_pos_local:
            logger.debug("%s: mainloop next status or next exe_pos", self.uid)
            # the game has changed to the next status, while local status is behind
            # so, we should print some message
            self.round_status_local = round_status
            exe_player = self.game.players[exe_pos]
            # FIXME: The calculation is tedious and error prone
            bgame.send_private_msg_to_channel_by_table_id(
                self.uid, exe_player.userid, None, build_prompt_payload(
                    exe_player.cards, exe_player.get_remaining_chip(), self.game.highest_bet - exe_player.chip_bet,
                    self.game.mini_raise + self.game.last_round_bet - exe_player.chip_bet
                ))

        else:
            logger.debug("%s: mainloop decrease countdown %d", self.uid, self.countdown)
            # neither the game stage nor current active player are changed
            # so, we should update the message and decrease the countdown
            self.countdown -= 1
            if self.msg_ts != "":
                bgame.update_msg_by_table_id(
                    self.uid, self.msg_ts, blocks=self._get_payload(self.game.round_status, self.is_stall_payload))

        if not self.game.players[self.game.exe_pos].is_normal():
            self.game.pfold(self.game.exe_pos)
            bgame.send_to_channel_by_table_id(
                self.uid, f"leaving: {self.players[exe_pos].username} fold")
            self.countdown = MAX_AWAIT
            return False

        self.exe_pos_local = exe_pos
        logger.debug("%s: mainloop end", self.uid)
        return False

    def _get_payload(self, round_status: lgame.RoundStatus, stall: bool):
        info_list = []
        pos = self.game.sb
        exe_pos = self.game.exe_pos
        ordered_players = self.players[pos:] + self.players[:pos]
        for player in ordered_players:
            if player.is_normal():
                action = self.game.round_actions[round_status.value].actions[player.userid]
                m_action = ""
                m_chip = 0
                if action.active and (stall or player != self.players[exe_pos]):
                    m_action = action.action
                    m_chip = action.chip

                if player.is_fold() and m_action != "fold":
                    continue

                info_list.append(build_info_str(
                    player.username, self.max_name_len, player.get_remaining_chip(), m_action, m_chip,
                    not stall and player == self.players[exe_pos], self.countdown))
        return build_payload(self.game.pub_cards, self.game.total_pot, self.game.ante,
                             self.players[self.game.btn].username, self.players[self.game.bb].username, self.players[self.game.sb].username, info_list)

    def update_payload(self, round_status: lgame.RoundStatus, stall: bool):
        """Update the message which we sent to the table to indicate the info of current round

        Args:
            round_status (RoundStatus): An enum which implies the round
            stall (bool): A flag which indicates whether the countdown should continue
        """
        old_ts = self.msg_ts
        self.countdown = MAX_AWAIT
        self.msg_ts, err = bgame.send_to_channel_by_table_id(
            self.uid, blocks=self._get_payload(round_status, stall))
        if err is not None:
            raise RuntimeError  # TODO: fix later
        self.is_stall_payload = stall
        if old_ts != "":
            bgame.delete_msg_by_table_id(self.uid, old_ts)

    def update_chip(self):
        for player in self.game.players:
            self.storage.change_table_chip(player.userid, self.uid, player.chip)
            if player.chip <= 0:
                logging.debug("%s has no chips(%d) and is about to leaving", player.username, player.chip)
                player.set_leaving()
                bgame.send_to_channel_by_table_id(
                    self.uid, f"{player.username} does not have any chip, and is leaving the table")

    def show_result(self, result: lgame.Result):
        players = self.game.players[self.game.last_aggressive:] + self.game.players[:self.game.last_aggressive]
        biggest_rank = players[0].rank
        for player in players:
            chip = result.chip_changes[player]
            act = "win" if chip >= 0 else "lose"
            hand = ""
            # if result.should_show_hand() and not player.is_fold() and player.rank >= biggest_rank:
            if result.should_show_hand() and not player.is_fold():
                biggest_rank = player.rank
                hand = f" ({card_to_emoji(str(player.cards[0]))}  {card_to_emoji(str(player.cards[1]))}) "

            bgame.send_to_channel_by_table_id(
                self.uid, f"{player.username} {hand} {act} {abs(chip)}, current chip: {player.chip}\n")

    def call_or_check(self, user_id) -> str:
        player_pos = self.players_user2pos[user_id]
        if self.game.is_check_permitted(player_pos):
            return self.check(user_id)
        return self.call(user_id)

    def check(self, user_id) -> str:
        player_pos = self.players_user2pos[user_id]
        if self.game.pcheck(player_pos) != 0:
            return f"{get_mentioned_string(user_id)}, invalid check"

    def fold(self, user_id) -> str:
        player_pos = self.players_user2pos[user_id]
        if self.game.pfold(player_pos) != 0:
            return f"{get_mentioned_string(user_id)}, invalid fold"

    def bet(self, user_id, chip: int) -> str:
        player_pos = self.players_user2pos[user_id]
        if self.game.praise(player_pos, chip) != 0:
            return f"{get_mentioned_string(user_id)}, invalid bet"

    def call(self, user_id) -> str:
        player_pos = self.players_user2pos[user_id]
        if self.game.pcall(player_pos) != 0:
            return f"{get_mentioned_string(user_id)}, invalid call"

    def all_in(self, user_id) -> str:
        player_pos = self.players_user2pos[user_id]
        if self.game.pallin(player_pos) != 0:
            return f"{get_mentioned_string(user_id)}, invalid all in"

    def get_game_info(self) -> str:
        info_str = f"{self.game.game_status.name} {self.game.get_round_status_name()}\n"
        info_str += f"btn: {self.game.btn} {get_mentioned_string(self.players[self.game.btn].userid)}\n"
        info_str += f"sb: {self.game.sb} {get_mentioned_string(self.players[self.game.sb].userid)}\n"
        info_str += f"bb: {self.game.bb} {get_mentioned_string(self.players[self.game.bb].userid)}\n"
        info_str += f"exe_pos: {self.game.exe_pos} {get_mentioned_string(self.players[self.game.exe_pos].userid)}\n"
        info_str += f"next_round: {self.game.next_round} {get_mentioned_string(self.players[self.game.next_round].userid)}\n"
        info_str += f"pub_card: {self.game.pub_cards}, highest_bet {self.game.highest_bet}\n"
        for pos, player in enumerate(self.game.players):
            info_str += f"{get_mentioned_string(player.userid)}: chip {player.chip}, "
            info_str += f"total_bet {player.chip_bet}, cards {player.cards}, "
            info_str += f"can_check {self.game.is_check_permitted(pos)}, "
            info_str += f"mode {player.mode.name}, status {player.status.name}, "
            info_str += f"rank {player.rank}, hand {player.hand}\n"
        return info_str
