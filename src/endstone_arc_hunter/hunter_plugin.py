"""
弧光猎手榜：PvP KD 统计 + 按 KD 授予猎杀手衔。
"""

import os
import time
from typing import Dict, List, Optional, Tuple

from endstone.command import Command, CommandSender
from endstone.event import ActorDamageEvent, PlayerDeathEvent, PlayerQuitEvent, event_handler
from endstone.form import ActionForm
from endstone.plugin import Plugin

from .DatabaseManager import DatabaseManager

# 最后攻击者归因窗口（秒）
ASSIST_WINDOW_SEC = 10.0

# 称号：按 KD 从低到高；(min_kd, max_kd_inclusive_or_None, title, rarity, description)
# max_kd 为 None 表示无上限
HUNTER_TITLE_TIERS: List[Tuple[float, Optional[float], str, str, str]] = [
    (0.00, 0.39, "人榜丙等猎杀者", "普通", "弧光猎手榜 · 人榜丙等"),
    (0.40, 0.69, "人榜乙等猎杀者", "稀有", "弧光猎手榜 · 人榜乙等"),
    (0.70, 0.99, "人榜甲等猎杀者", "稀有", "弧光猎手榜 · 人榜甲等"),
    (1.00, 1.99, "地榜丙等猎杀者", "史诗", "弧光猎手榜 · 地榜丙等"),
    (2.00, 2.99, "地榜乙等猎杀者", "史诗", "弧光猎手榜 · 地榜乙等"),
    (3.00, 4.99, "地榜甲等猎杀者", "传奇", "弧光猎手榜 · 地榜甲等"),
    (5.00, 6.99, "天榜丙等猎杀者", "传奇", "弧光猎手榜 · 天榜丙等"),
    (7.00, 9.99, "天榜乙等猎杀者", "神话", "弧光猎手榜 · 天榜乙等"),
    (10.00, None, "天榜甲等猎杀者", "神话", "弧光猎手榜 · 天榜甲等"),
]

ALL_HUNTER_TITLES = {t[2] for t in HUNTER_TITLE_TIERS}

# 与 arc_core TitleSystem.RARITY_COLORS 保持一致
RARITY_COLORS = {
    "普通": "§h",
    "稀有": "§9",
    "史诗": "§u",
    "传奇": "§6",
    "神话": "§c",
}


class ARCHunterPlugin(Plugin):
    prefix = "ARCHunter"
    api_version = "0.10"
    load = "POSTWORLD"

    commands = {
        "hunter": {
            "description": "查看弧光猎手榜 / 个人 KD",
            "usages": ["/hunter"],
            "permissions": ["arc_hunter.command.hunter"],
        },
    }

    permissions = {
        "arc_hunter.command.hunter": {
            "description": "允许使用 /hunter",
            "default": True,
        },
    }

    def on_load(self) -> None:
        self.logger.info("[ARCHunter] on_load")
        data_dir = os.path.join("plugins", "ARCHunter")
        os.makedirs(data_dir, exist_ok=True)
        self.db = DatabaseManager(os.path.join(data_dir, "hunter.db"))
        self._create_tables()
        # victim_xuid -> (attacker_xuid, attacker_name, timestamp)
        self._last_attackers: Dict[str, Tuple[str, str, float]] = {}
        self.arc = None

    def on_enable(self) -> None:
        self.logger.info("[ARCHunter] on_enable")
        self.register_events(self)
        self.arc = self.server.plugin_manager.get_plugin("arc_core")
        if self.arc is None:
            self.logger.warning("[ARCHunter] 未找到 arc_core，头衔功能将不可用（KD 仍会记录）")
        else:
            self._ensure_hunter_titles()
            self.logger.info("[ARCHunter] 已连接 arc_core，猎杀手衔已注册")

    def on_disable(self) -> None:
        self.logger.info("[ARCHunter] on_disable")
        if hasattr(self, "db"):
            self.db.close()

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "hunter":
            return True
        if not hasattr(sender, "xuid"):
            sender.send_message("§c该命令仅玩家可用。")
            return True
        self._show_hunter_panel(sender)
        return True

    # ------------------------------------------------------------------ DB
    def _create_tables(self) -> None:
        ok = self.db.create_table(
            "player_kd",
            {
                "xuid": "TEXT PRIMARY KEY",
                "name": "TEXT NOT NULL",
                "kills": "INTEGER NOT NULL DEFAULT 0",
                "deaths": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "REAL NOT NULL",
            },
        )
        if ok:
            self.logger.info("[ARCHunter] player_kd 表就绪")
        else:
            self.logger.error("[ARCHunter] 创建 player_kd 表失败")

    def _ensure_player_row(self, xuid: str, name: str) -> None:
        row = self.db.query_one("SELECT xuid FROM player_kd WHERE xuid = ?", (xuid,))
        if row is None:
            self.db.execute(
                "INSERT INTO player_kd (xuid, name, kills, deaths, updated_at) VALUES (?, ?, 0, 0, ?)",
                (xuid, name, time.time()),
            )
        else:
            self.db.execute(
                "UPDATE player_kd SET name = ?, updated_at = ? WHERE xuid = ?",
                (name, time.time(), xuid),
            )

    def _add_kill(self, xuid: str, name: str) -> Tuple[int, int]:
        self._ensure_player_row(xuid, name)
        self.db.execute(
            "UPDATE player_kd SET kills = kills + 1, name = ?, updated_at = ? WHERE xuid = ?",
            (name, time.time(), xuid),
        )
        return self._get_kd_counts(xuid)

    def _add_death(self, xuid: str, name: str) -> Tuple[int, int]:
        self._ensure_player_row(xuid, name)
        self.db.execute(
            "UPDATE player_kd SET deaths = deaths + 1, name = ?, updated_at = ? WHERE xuid = ?",
            (name, time.time(), xuid),
        )
        return self._get_kd_counts(xuid)

    def _get_kd_counts(self, xuid: str) -> Tuple[int, int]:
        row = self.db.query_one("SELECT kills, deaths FROM player_kd WHERE xuid = ?", (xuid,))
        if not row:
            return 0, 0
        return int(row["kills"] or 0), int(row["deaths"] or 0)

    def _get_player_stats(self, xuid: str) -> Optional[dict]:
        return self.db.query_one(
            "SELECT xuid, name, kills, deaths FROM player_kd WHERE xuid = ?", (xuid,)
        )

    def _get_top_players(self, limit: int = 10) -> List[dict]:
        # 按 KD 降序；死亡为 0 时用 kills 作为 KD；同 KD 按击杀多优先
        rows = self.db.query_all(
            """
            SELECT xuid, name, kills, deaths,
                   CASE WHEN deaths <= 0 THEN CAST(kills AS REAL)
                        ELSE CAST(kills AS REAL) / CAST(deaths AS REAL)
                   END AS kd
            FROM player_kd
            WHERE kills > 0 OR deaths > 0
            ORDER BY kd DESC, kills DESC, deaths ASC
            LIMIT ?
            """,
            (limit,),
        )
        return rows or []

    def _is_on_leaderboard(self, xuid: str) -> bool:
        kills, deaths = self._get_kd_counts(xuid)
        return kills > 0 or deaths > 0

    def _color_for_rarity(self, rarity: str) -> str:
        if self.arc is not None:
            title_system = getattr(self.arc, "title_system", None)
            if title_system is not None:
                try:
                    return title_system.get_title_rarity_color("", rarity)
                except Exception:
                    pass
        r = str(rarity or "普通").strip()
        if r == "传说":
            r = "传奇"
        return RARITY_COLORS.get(r, "§f")

    def _format_title(self, title: str, rarity: str) -> str:
        return f"{self._color_for_rarity(rarity)}{title}§r"

    def _format_top_players_lines(self, limit: int = 10, highlight_xuid: str = "") -> List[str]:
        lines = ["§f—— 全服 KD 前 10 ——§r"]
        top = self._get_top_players(limit)
        if not top:
            lines.append("§f暂无数据")
        else:
            for i, row in enumerate(top, start=1):
                rk = int(row.get("kills") or 0)
                rd = int(row.get("deaths") or 0)
                rkd = float(row.get("kd") or self.calc_kd(rk, rd))
                rname = row.get("name") or "?"
                rtitle, rrarity = self.title_info_for_kd(rkd)
                colored_title = self._format_title(rtitle, rrarity)
                mark = " §f◀你" if highlight_xuid and str(row.get("xuid")) == highlight_xuid else ""
                lines.append(
                    f"§f#{i} {rname}  {rk}/{rd}  {rkd:.2f}  {colored_title}{mark}"
                )
        return lines

    def _broadcast(self, text: str) -> None:
        try:
            self.server.broadcast_message(text)
        except Exception as e:
            self.logger.warning(f"[ARCHunter] 播报失败: {e}")

    def _broadcast_lines(self, lines: List[str]) -> None:
        for line in lines:
            stripped = str(line or "").strip()
            if stripped:
                self._broadcast(stripped)

    def _broadcast_newcomer(self, name: str) -> None:
        self._broadcast(f"§f[猎手榜] {name} 加入了猎手榜！")

    def _broadcast_leaderboard(self) -> None:
        lines = ["§f弧光猎手榜  榜单已更新"]
        lines.extend(self._format_top_players_lines(10))
        self._broadcast_lines(lines)

    def _notify_leaderboard_changes(
        self,
        newcomers: List[str],
    ) -> None:
        seen = set()
        for name in newcomers:
            key = str(name or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            self._broadcast_newcomer(key)
        self._broadcast_leaderboard()

    @staticmethod
    def calc_kd(kills: int, deaths: int) -> float:
        if deaths <= 0:
            return float(kills)
        return float(kills) / float(deaths)

    @staticmethod
    def title_info_for_kd(kd: float) -> Tuple[str, str]:
        for min_kd, max_kd, title, rarity, _desc in HUNTER_TITLE_TIERS:
            if kd + 1e-9 < min_kd:
                continue
            if max_kd is None or kd <= max_kd + 1e-9:
                return title, rarity
        first = HUNTER_TITLE_TIERS[0]
        return first[2], first[3]

    @staticmethod
    def title_for_kd(kd: float) -> str:
        return ARCHunterPlugin.title_info_for_kd(kd)[0]

    # ------------------------------------------------------------------ Titles
    def _ensure_hunter_titles(self) -> None:
        if self.arc is None:
            return
        for _min, _max, title, rarity, desc in HUNTER_TITLE_TIERS:
            try:
                self.arc.api_ensure_title_definition(
                    title,
                    rarity=rarity,
                    description=desc,
                    reward_money=0.0,
                    reward_items=[],
                )
            except Exception as e:
                self.logger.warning(f"[ARCHunter] 注册头衔失败 {title}: {e}")

    def _sync_hunter_title(self, xuid: str, player=None) -> None:
        """按当前 KD 撤销其它猎手称号，解锁并（若合适）佩戴对应称号。"""
        if self.arc is None:
            return
        kills, deaths = self._get_kd_counts(xuid)
        if kills <= 0 and deaths <= 0:
            return
        kd = self.calc_kd(kills, deaths)
        target_title = self.title_for_kd(kd)

        title_system = getattr(self.arc, "title_system", None)
        equipped = ""
        try:
            equipped = self.arc.api_get_equipped_title(xuid=xuid) or ""
        except Exception:
            equipped = ""

        # 撤销其它猎手称号
        if title_system is not None:
            for t in ALL_HUNTER_TITLES:
                if t == target_title:
                    continue
                try:
                    title_system.revoke_title_by_xuid(xuid, t)
                except Exception:
                    pass

        # 解锁目标称号
        online = player
        if online is None:
            online = self._find_online_by_xuid(xuid)
        try:
            if online is not None:
                self.arc.api_unlock_title(online, target_title)
            else:
                self.arc.api_unlock_title_by_xuid(xuid, target_title)
        except Exception as e:
            self.logger.warning(f"[ARCHunter] 解锁头衔失败: {e}")
            return

        # 若当前未佩戴，或佩戴的是旧猎手称号，则换成新称号
        should_equip = (not equipped) or (equipped in ALL_HUNTER_TITLES)
        if should_equip and title_system is not None and online is not None:
            try:
                title_system.set_equipped_title(online, target_title)
                update_tag = getattr(self.arc, "_update_player_name_tag", None)
                if callable(update_tag):
                    update_tag(online)
            except Exception as e:
                self.logger.warning(f"[ARCHunter] 佩戴头衔失败: {e}")

    def _find_online_by_xuid(self, xuid: str):
        for p in self.server.online_players:
            if str(getattr(p, "xuid", "")) == str(xuid):
                return p
        return None

    # ------------------------------------------------------------------ Events
    @event_handler
    def on_actor_damage(self, event: ActorDamageEvent):
        try:
            victim = event.actor
            if victim is None or getattr(victim, "type", None) != "minecraft:player":
                return
            damage_source = getattr(event, "damage_source", None)
            attacker = getattr(damage_source, "actor", None) if damage_source else None
            if attacker is None or getattr(attacker, "type", None) != "minecraft:player":
                return

            victim_xuid = str(getattr(victim, "xuid", "") or "")
            attacker_xuid = str(getattr(attacker, "xuid", "") or "")
            if not victim_xuid or not attacker_xuid or victim_xuid == attacker_xuid:
                return

            attacker_name = str(getattr(attacker, "name", "") or attacker_xuid)
            self._last_attackers[victim_xuid] = (attacker_xuid, attacker_name, time.time())
        except Exception as e:
            self.logger.warning(f"[ARCHunter] on_actor_damage 异常: {e}")

    @event_handler
    def on_player_death(self, event: PlayerDeathEvent):
        try:
            victim = event.player
            if victim is None:
                return
            victim_xuid = str(getattr(victim, "xuid", "") or "")
            victim_name = str(getattr(victim, "name", "") or victim_xuid)
            if not victim_xuid:
                return

            killer_xuid, killer_name = self._resolve_killer(event, victim_xuid)
            # 仅当存在玩家击杀者（直接击杀或 10 秒内最后攻击）时记 KD
            if killer_xuid and killer_xuid != victim_xuid:
                killer_was_on_board = self._is_on_leaderboard(killer_xuid)
                victim_was_on_board = self._is_on_leaderboard(victim_xuid)

                self._add_death(victim_xuid, victim_name)
                self._sync_hunter_title(victim_xuid, player=victim)

                self._add_kill(killer_xuid, killer_name)
                killer_online = self._find_online_by_xuid(killer_xuid)
                self._sync_hunter_title(killer_xuid, player=killer_online)

                newcomers: List[str] = []
                if not killer_was_on_board:
                    newcomers.append(killer_name)
                if not victim_was_on_board:
                    newcomers.append(victim_name)
                self._notify_leaderboard_changes(newcomers)

                if killer_online is not None:
                    k, d = self._get_kd_counts(killer_xuid)
                    kd = self.calc_kd(k, d)
                    ktitle, krarity = self.title_info_for_kd(kd)
                    killer_online.send_message(
                        f"§f[猎手榜] 击杀 {victim_name} | K/D {k}/{d} ({kd:.2f}) → {self._format_title(ktitle, krarity)}"
                    )
                victim.send_message(
                    f"§f[猎手榜] 你被 {killer_name} 击杀"
                )

            self._last_attackers.pop(victim_xuid, None)
        except Exception as e:
            self.logger.warning(f"[ARCHunter] on_player_death 异常: {e}")

    def _resolve_killer(self, event: PlayerDeathEvent, victim_xuid: str) -> Tuple[str, str]:
        """优先用 damage_source 的玩家击杀者；否则用 10 秒内最后攻击者。"""
        # 1) 直接击杀者
        try:
            damage_source = getattr(event, "damage_source", None)
            killer = getattr(damage_source, "actor", None) if damage_source else None
            if killer is not None and getattr(killer, "type", None) == "minecraft:player":
                kx = str(getattr(killer, "xuid", "") or "")
                kn = str(getattr(killer, "name", "") or kx)
                if kx and kx != victim_xuid:
                    return kx, kn
        except Exception:
            pass

        # 2) 10 秒内最后攻击者（摔死/火烧等）
        info = self._last_attackers.get(victim_xuid)
        if info:
            ax, an, ts = info
            if time.time() - ts <= ASSIST_WINDOW_SEC and ax and ax != victim_xuid:
                return ax, an
        return "", ""

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent):
        try:
            xuid = str(getattr(event.player, "xuid", "") or "")
            if xuid:
                self._last_attackers.pop(xuid, None)
        except Exception:
            pass

    # ------------------------------------------------------------------ UI
    def _build_hunter_panel_content(self, xuid: str) -> str:
        kills, deaths = self._get_kd_counts(xuid)
        kd = self.calc_kd(kills, deaths)
        if kills > 0 or deaths > 0:
            title, rarity = self.title_info_for_kd(kd)
            title_text = self._format_title(title, rarity)
        else:
            title_text = "§f（暂无）"

        lines = [
            f"§f你的战绩：{kills} 杀 / {deaths} 死  KD={kd:.2f}",
            f"§f当前称号：{title_text}",
            "",
        ]
        lines.extend(self._format_top_players_lines(10, highlight_xuid=xuid))
        return "\n".join(lines)

    def _show_hunter_panel(self, player) -> None:
        xuid = str(player.xuid)
        name = str(player.name)
        self._ensure_player_row(xuid, name)
        panel = ActionForm(
            title="§f弧光猎手榜",
            content=self._build_hunter_panel_content(xuid),
            on_close=None,
        )
        player.send_form(panel)
