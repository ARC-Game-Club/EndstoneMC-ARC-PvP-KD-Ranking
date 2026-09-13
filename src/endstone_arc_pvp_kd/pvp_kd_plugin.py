"""
弧光 PvP KD 排行榜插件：PvP KD 统计 + 按 KD 授予 PvP 头衔。
"""

import os
import shutil
import time
from typing import Dict, List, Optional, Tuple

from endstone.command import Command, CommandSender
from endstone.event import ActorDamageEvent, PlayerDeathEvent, PlayerJoinEvent, PlayerQuitEvent, event_handler
from endstone.form import ActionForm
from endstone.plugin import Plugin

from .DatabaseManager import DatabaseManager

PLUGIN_DISPLAY_NAME = "弧光 PvP KD 排行榜"
LOG_PREFIX = "[ARCPvPKD]"
BROADCAST_TAG = "PvP KD榜"

# 最后攻击者归因窗口（秒）
ASSIST_WINDOW_SEC = 10.0
# 进服后延迟再广播榜单（tick），让进服玩家也能看到
JOIN_BROADCAST_DELAY_TICKS = 40

# 称号：按 KD 从低到高；(min_kd, max_kd_inclusive_or_None, title, rarity, description)
# 品质规则：最高档（天榜甲等）= 神话（红）；其余 8 档每两档共一品质（普通→稀有→史诗→传奇）
PVP_KD_TITLE_TIERS: List[Tuple[float, Optional[float], str, str, str]] = [
    (0.00, 0.39, "人榜丙等猎杀者", "普通", f"{PLUGIN_DISPLAY_NAME} · 人榜丙等"),
    (0.40, 0.69, "人榜乙等猎杀者", "普通", f"{PLUGIN_DISPLAY_NAME} · 人榜乙等"),
    (0.70, 0.99, "人榜甲等猎杀者", "稀有", f"{PLUGIN_DISPLAY_NAME} · 人榜甲等"),
    (1.00, 1.99, "地榜丙等猎杀者", "稀有", f"{PLUGIN_DISPLAY_NAME} · 地榜丙等"),
    (2.00, 2.99, "地榜乙等猎杀者", "史诗", f"{PLUGIN_DISPLAY_NAME} · 地榜乙等"),
    (3.00, 4.99, "地榜甲等猎杀者", "史诗", f"{PLUGIN_DISPLAY_NAME} · 地榜甲等"),
    (5.00, 6.99, "天榜丙等猎杀者", "传奇", f"{PLUGIN_DISPLAY_NAME} · 天榜丙等"),
    (7.00, 9.99, "天榜乙等猎杀者", "传奇", f"{PLUGIN_DISPLAY_NAME} · 天榜乙等"),
    (10.00, None, "天榜甲等猎杀者", "神话", f"{PLUGIN_DISPLAY_NAME} · 天榜甲等"),
]

ALL_PVP_KD_TITLES = {t[2] for t in PVP_KD_TITLE_TIERS}
PVP_KD_TITLE_RARITY_MAP = {t[2]: t[3] for t in PVP_KD_TITLE_TIERS}
PVP_KD_TITLE_DESC_MAP = {t[2]: t[4] for t in PVP_KD_TITLE_TIERS}

# 与 arc_core TitleSystem.RARITY_COLORS 保持一致
RARITY_COLORS = {
    "普通": "§h",
    "稀有": "§9",
    "史诗": "§u",
    "传奇": "§6",
    "神话": "§c",
}


class ARCPvPKDPlugin(Plugin):
    prefix = "ARCPvPKD"
    api_version = "0.10"
    load = "POSTWORLD"

    commands = {
        "kd": {
            "description": f"查看{PLUGIN_DISPLAY_NAME} / 个人 KD",
            "usages": ["/kd"],
            "permissions": ["arc_pvp_kd.command.kd"],
        },
    }

    permissions = {
        "arc_pvp_kd.command.kd": {
            "description": "允许使用 /kd",
            "default": True,
        },
    }

    def on_load(self) -> None:
        self.logger.info(f"{LOG_PREFIX} on_load")
        data_dir = os.path.join("plugins", "ARCPvPKD")
        os.makedirs(data_dir, exist_ok=True)
        db_path = os.path.join(data_dir, "pvp_kd.db")
        self._migrate_legacy_database(db_path)
        self.db = DatabaseManager(db_path)
        self._create_tables()
        self._last_attackers: Dict[str, Tuple[str, str, float]] = {}
        self.arc = None

    @staticmethod
    def _migrate_legacy_database(new_db_path: str) -> None:
        """从旧版 arc_hunter（ARCHunter/hunter.db）迁移数据。"""
        if os.path.exists(new_db_path):
            return
        legacy_db = os.path.join("plugins", "ARCHunter", "hunter.db")
        if os.path.exists(legacy_db):
            shutil.copy2(legacy_db, new_db_path)

    def on_enable(self) -> None:
        self.logger.info(f"{LOG_PREFIX} on_enable")
        self.register_events(self)
        self.arc = self.server.plugin_manager.get_plugin("arc_core")
        if self.arc is None:
            self.logger.warning(f"{LOG_PREFIX} 未找到 arc_core，头衔功能将不可用（KD 仍会记录）")
        else:
            self._migrate_pvp_title_rarities()
            self._ensure_pvp_kd_titles()
            self._register_arc_main_menu_button()
            self._resync_all_pvp_kd_titles()
            self.logger.info(f"{LOG_PREFIX} 已连接 arc_core，PvP KD 头衔已注册")

    def on_disable(self) -> None:
        self.logger.info(f"{LOG_PREFIX} on_disable")
        try:
            arc = getattr(self, "arc", None) or self.server.plugin_manager.get_plugin("arc_core")
            if arc is not None and hasattr(arc, "api_unregister_main_menu_button"):
                arc.api_unregister_main_menu_button("arc_pvp_kd:main")
        except Exception:
            pass
        if hasattr(self, "db"):
            self.db.close()

    def _register_arc_main_menu_button(self) -> None:
        arc = getattr(self, "arc", None)
        if arc is None or not hasattr(arc, "api_register_main_menu_button"):
            return
        try:
            ok = arc.api_register_main_menu_button(
                "arc_pvp_kd:main",
                "PvP KD 排行榜",
                on_click=self._show_ranking_panel,
                priority=6,
                icon="textures/arc_core/pvp_kd.png",
            )
            if ok:
                self.logger.info(f"{LOG_PREFIX} 已注册 ARC 主菜单按钮")
        except Exception as e:
            self.logger.warning(f"{LOG_PREFIX} 注册 ARC 主菜单按钮失败: {e}")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "kd":
            return True
        if not hasattr(sender, "xuid"):
            sender.send_message("§c该命令仅玩家可用。")
            return True
        self._show_ranking_panel(sender)
        return True

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
            self.logger.info(f"{LOG_PREFIX} player_kd 表就绪")
        else:
            self.logger.error(f"{LOG_PREFIX} 创建 player_kd 表失败")

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

    def _get_top_players(self, limit: int = 10) -> List[dict]:
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
                    f"§f#{i} {rname}  {rk}/{rd}  {self.format_kd(rk, rd)}  {colored_title}{mark}"
                )
        return lines

    def _broadcast(self, text: str) -> None:
        try:
            self.server.broadcast_message(text)
        except Exception as e:
            self.logger.warning(f"{LOG_PREFIX} 播报失败: {e}")

    def _broadcast_lines(self, lines: List[str]) -> None:
        for line in lines:
            stripped = str(line or "").strip()
            if stripped:
                self._broadcast(stripped)

    def _broadcast_newcomer(self, name: str) -> None:
        self._broadcast(f"§f[{BROADCAST_TAG}] {name} 加入了 PvP KD 排行榜！")

    def _broadcast_leaderboard(self) -> None:
        lines = [f"§f{PLUGIN_DISPLAY_NAME}  榜单已更新"]
        lines.extend(self._format_top_players_lines(10))
        self._broadcast_lines(lines)

    def _broadcast_leaderboard_on_join(self, joiner_name: str = "") -> None:
        name = str(joiner_name or "").strip()
        if name:
            header = f"§f[{BROADCAST_TAG}] {name} 进服 · 当前榜单"
        else:
            header = f"§f[{BROADCAST_TAG}] 当前榜单"
        lines = [header]
        lines.extend(self._format_top_players_lines(10))
        self._broadcast_lines(lines)

    def _notify_leaderboard_changes(self, newcomers: List[str]) -> None:
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
        """数值 KD（用于排序/头衔）。0 死有杀按击杀数近似，非展示用。"""
        if kills <= 0 and deaths <= 0:
            return 0.0
        if deaths <= 0:
            return float(kills)
        return float(kills) / float(deaths)

    @staticmethod
    def format_kd(kills: int, deaths: int) -> str:
        """展示用 KD：0-0 为 -；有杀无死为 ∞；其余两位小数。"""
        if kills <= 0 and deaths <= 0:
            return "-"
        if deaths <= 0:
            return "∞"
        return f"{float(kills) / float(deaths):.2f}"

    @staticmethod
    def title_info_for_kd(kd: float) -> Tuple[str, str]:
        for min_kd, max_kd, title, rarity, _desc in PVP_KD_TITLE_TIERS:
            if kd + 1e-9 < min_kd:
                continue
            if max_kd is None or kd <= max_kd + 1e-9:
                return title, rarity
        first = PVP_KD_TITLE_TIERS[0]
        return first[2], first[3]

    @staticmethod
    def title_for_kd(kd: float) -> str:
        return ARCPvPKDPlugin.title_info_for_kd(kd)[0]

    def _ensure_pvp_kd_titles(self) -> None:
        if self.arc is None:
            return
        for _min, _max, title, rarity, desc in PVP_KD_TITLE_TIERS:
            try:
                self.arc.api_set_title_definition(
                    title,
                    rarity=rarity,
                    description=desc,
                    reward_money=0.0,
                    reward_items=[],
                )
            except Exception as e:
                self.logger.warning(f"{LOG_PREFIX} 注册头衔失败 {title}: {e}")

    def _migrate_pvp_title_rarities(self) -> None:
        """将 arc_core 头衔库中 PvP 榜头衔的定义/解锁/佩戴统一为正确稀有度。"""
        if self.arc is None:
            return
        title_system = getattr(self.arc, "title_system", None)
        if title_system is None:
            return
        db = title_system.database_manager
        migrated_defs = 0
        migrated_unlocks = 0
        migrated_equipped = 0

        for title, target_rarity in PVP_KD_TITLE_RARITY_MAP.items():
            rows = db.query_all(
                "SELECT title, rarity, description, reward_money, reward_items "
                "FROM title_definitions WHERE title = ?",
                (title,),
            ) or []

            description = PVP_KD_TITLE_DESC_MAP.get(title, "")
            reward_money = 0.0
            reward_items = "[]"
            for row in rows:
                row_rarity = str(row.get("rarity") or "").strip()
                row_desc = str(row.get("description") or "").strip()
                if row_rarity == target_rarity:
                    if row_desc:
                        description = row_desc
                    reward_money = float(row.get("reward_money") or 0.0)
                    reward_items = row.get("reward_items") or "[]"
                elif not description and row_desc:
                    description = row_desc

            db.execute(
                "INSERT OR REPLACE INTO title_definitions "
                "(title, rarity, description, reward_money, reward_items) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, target_rarity, description, reward_money, reward_items),
            )
            removed_defs = db.execute(
                "DELETE FROM title_definitions WHERE title = ? AND rarity != ?",
                (title, target_rarity),
            )
            if removed_defs:
                migrated_defs += int(removed_defs)

            unlock_rows = db.query_all(
                "SELECT xuid, rarity, unlocked_at FROM player_title_unlock_time WHERE title = ?",
                (title,),
            ) or []
            if unlock_rows:
                earliest_by_xuid: Dict[str, str] = {}
                for row in unlock_rows:
                    xuid = str(row.get("xuid") or "").strip()
                    if not xuid:
                        continue
                    unlocked_at = str(row.get("unlocked_at") or "")
                    prev = earliest_by_xuid.get(xuid)
                    if prev is None or (unlocked_at and unlocked_at < prev):
                        earliest_by_xuid[xuid] = unlocked_at

                removed_unlocks = db.execute(
                    "DELETE FROM player_title_unlock_time WHERE title = ?",
                    (title,),
                )
                if removed_unlocks:
                    migrated_unlocks += int(removed_unlocks)

                for xuid, unlocked_at in earliest_by_xuid.items():
                    db.execute(
                        "INSERT OR IGNORE INTO player_title_unlock_time "
                        "(xuid, title, rarity, unlocked_at) VALUES (?, ?, ?, ?)",
                        (xuid, title, target_rarity, unlocked_at or None),
                    )

            updated_equipped = db.execute(
                "UPDATE player_title_equipped SET rarity = ? "
                "WHERE title = ? AND COALESCE(rarity, '') != ?",
                (target_rarity, title, target_rarity),
            )
            if updated_equipped:
                migrated_equipped += int(updated_equipped)

        if migrated_defs or migrated_unlocks or migrated_equipped:
            self.logger.info(
                f"{LOG_PREFIX} 头衔稀有度迁移：删除多余定义 {migrated_defs} 条，"
                f"整理解锁 {migrated_unlocks} 条，修正佩戴 {migrated_equipped} 条"
            )

    def _resync_all_pvp_kd_titles(self) -> None:
        rows = self.db.query_all(
            "SELECT xuid FROM player_kd WHERE kills > 0 OR deaths > 0",
            (),
        ) or []
        for row in rows:
            xuid = str(row.get("xuid") or "").strip()
            if xuid:
                self._sync_pvp_kd_title(xuid)

    def _sync_pvp_kd_title(self, xuid: str, player=None) -> None:
        if self.arc is None:
            return
        kills, deaths = self._get_kd_counts(xuid)
        if kills <= 0 and deaths <= 0:
            return
        kd = self.calc_kd(kills, deaths)
        target_title, target_rarity = self.title_info_for_kd(kd)

        title_system = getattr(self.arc, "title_system", None)
        equipped = ""
        try:
            equipped = self.arc.api_get_equipped_title(xuid=xuid) or ""
        except Exception:
            equipped = ""

        if title_system is not None:
            for t in ALL_PVP_KD_TITLES:
                if t == target_title:
                    continue
                try:
                    title_system.revoke_title_by_xuid(xuid, t)
                except Exception:
                    pass

        online = player
        if online is None:
            online = self._find_online_by_xuid(xuid)
        try:
            if online is not None:
                self.arc.api_unlock_title(online, target_title, rarity=target_rarity)
            else:
                self.arc.api_unlock_title_by_xuid(xuid, target_title, rarity=target_rarity)
        except Exception as e:
            self.logger.warning(f"{LOG_PREFIX} 解锁头衔失败: {e}")
            return

        should_equip = (not equipped) or (equipped in ALL_PVP_KD_TITLES)
        if should_equip and title_system is not None and online is not None:
            try:
                title_system.set_equipped_title(online, target_title, target_rarity)
                update_tag = getattr(self.arc, "_update_player_name_tag", None)
                if callable(update_tag):
                    update_tag(online)
            except Exception as e:
                self.logger.warning(f"{LOG_PREFIX} 佩戴头衔失败: {e}")

    def _find_online_by_xuid(self, xuid: str):
        for p in self.server.online_players:
            if str(getattr(p, "xuid", "")) == str(xuid):
                return p
        return None

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
            self.logger.warning(f"{LOG_PREFIX} on_actor_damage 异常: {e}")

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
            if killer_xuid and killer_xuid != victim_xuid:
                killer_was_on_board = self._is_on_leaderboard(killer_xuid)
                victim_was_on_board = self._is_on_leaderboard(victim_xuid)

                self._add_death(victim_xuid, victim_name)
                self._sync_pvp_kd_title(victim_xuid, player=victim)

                self._add_kill(killer_xuid, killer_name)
                killer_online = self._find_online_by_xuid(killer_xuid)
                self._sync_pvp_kd_title(killer_xuid, player=killer_online)

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
                        f"§f[{BROADCAST_TAG}] 击杀 {victim_name} | K/D {k}/{d} ({self.format_kd(k, d)}) → {self._format_title(ktitle, krarity)}"
                    )
                victim.send_message(
                    f"§f[{BROADCAST_TAG}] 你被 {killer_name} 击杀"
                )

            self._last_attackers.pop(victim_xuid, None)
        except Exception as e:
            self.logger.warning(f"{LOG_PREFIX} on_player_death 异常: {e}")

    def _resolve_killer(self, event: PlayerDeathEvent, victim_xuid: str) -> Tuple[str, str]:
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

        info = self._last_attackers.get(victim_xuid)
        if info:
            ax, an, ts = info
            if time.time() - ts <= ASSIST_WINDOW_SEC and ax and ax != victim_xuid:
                return ax, an
        return "", ""

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent):
        try:
            name = str(getattr(event.player, "name", "") or "")
            self.server.scheduler.run_task(
                self,
                lambda n=name: self._broadcast_leaderboard_on_join(n),
                delay=JOIN_BROADCAST_DELAY_TICKS,
            )
        except Exception as e:
            self.logger.warning(f"{LOG_PREFIX} on_player_join 异常: {e}")

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent):
        try:
            xuid = str(getattr(event.player, "xuid", "") or "")
            if xuid:
                self._last_attackers.pop(xuid, None)
        except Exception:
            pass

    def _build_ranking_panel_content(self, xuid: str) -> str:
        kills, deaths = self._get_kd_counts(xuid)
        kd = self.calc_kd(kills, deaths)
        if kills > 0 or deaths > 0:
            title, rarity = self.title_info_for_kd(kd)
            title_text = self._format_title(title, rarity)
        else:
            title_text = "§f（暂无）"

        lines = [
            f"§f你的战绩：{kills} 杀 / {deaths} 死  KD={self.format_kd(kills, deaths)}",
            f"§f当前称号：{title_text}",
            "",
        ]
        lines.extend(self._format_top_players_lines(10, highlight_xuid=xuid))
        return "\n".join(lines)

    def _show_ranking_panel(self, player) -> None:
        xuid = str(player.xuid)
        name = str(player.name)
        self._ensure_player_row(xuid, name)
        panel = ActionForm(
            title=f"§f{PLUGIN_DISPLAY_NAME}",
            content=self._build_ranking_panel_content(xuid),
            on_close=None,
        )
        player.send_form(panel)
