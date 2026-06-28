import os
import asyncio
import logging
from datetime import datetime, timedelta
import sqlite3
import discord
from discord.ext import commands
from apscheduler.events import EVENT_JOB_REMOVED, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, EVENT_JOB_MODIFIED
from utils import JST

logger = logging.getLogger(__name__)

# --- 設定定数 ---
HISTORY_RETENTION_DAYS = 3  # 履歴を保持する日数（3日経ったら自動削除）
SYNC_DELAY_SECONDS = 10     # 同期の待機時間（秒）。頻繁なアップロードを抑える。

# ギルドごとのストレージ情報を保持するクラス
class GuildStorage:
    def __init__(self, bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.storage_channel = None
        self._sync_wait_task = None
        self.db_file = os.path.join(bot.base_dir, f"jobs_{guild.id}.sqlite")
        self.init_db()

    def init_db(self):
        """SQLiteに履歴テーブルを作成する"""
        try:
            with sqlite3.connect(self.db_file) as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS history 
                                (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                                 user_id TEXT, time TEXT, days TEXT, 
                                 set_at TEXT, category TEXT)''')
                conn.commit()
        except Exception as e:
            logger.error(f"[{self.guild.name}] Database initialization failed: {e}")

    def get_history(self, user_id=None):
        """DBから履歴を取得する"""
        try:
            with sqlite3.connect(self.db_file) as conn:
                conn.row_factory = sqlite3.Row
                if user_id:
                    cur = conn.execute("SELECT * FROM history WHERE user_id = ? ORDER BY set_at DESC LIMIT 100", (str(user_id),))
                else:
                    cur = conn.execute("SELECT * FROM history ORDER BY set_at DESC LIMIT 5")
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"[{self.guild.name}] Failed to get history: {e}")
            return []

    def add_history(self, user_id, time, days, category):
        """履歴をDBに直接挿入し、古いデータをクリーンアップする。重複記録を回避するため直近10秒の同じ内容はスキップ"""
        now = datetime.now(JST)
        set_at = now.isoformat()
        threshold = (now - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
        # 重複チェック用：直近10秒以内の同じユーザー・カテゴリの記録があるか確認
        recent_threshold = (now - timedelta(seconds=10)).isoformat()

        try:
            with sqlite3.connect(self.db_file) as conn:
                # 重複チェック
                cur = conn.execute("""
                    SELECT COUNT(*) FROM history 
                    WHERE user_id = ? AND category = ? AND time = ? AND set_at > ?
                """, (str(user_id), category, time, recent_threshold))
                if cur.fetchone()[0] > 0:
                    logger.info(f"[{self.guild.name}] 重複記録をスキップ: {user_id} {category} {time}")
                    return
                
                # 重複がなければ新規追加
                conn.execute("INSERT INTO history (user_id, time, days, set_at, category) VALUES (?, ?, ?, ?, ?)",
                             (str(user_id), time, days, set_at, category))
                # 3日以上前のデータを削除
                conn.execute("DELETE FROM history WHERE set_at < ?", (threshold,))
                conn.commit()
            self.request_sync()
        except Exception as e:
            logger.error(f"[{self.guild.name}] Error in add_history: {e}")

    def request_sync(self):
        """同期（バックアップ）を依頼する。連続した依頼は指定秒数待ってから1回にまとめる。"""
        async def delayed_sync():
            await asyncio.sleep(SYNC_DELAY_SECONDS)
            # 同期前にストレージチャンネルが存在するか確認してからアップロード
            await self.ensure_storage_channel()
            await self.upload_data_to_channel()

        if self._sync_wait_task and not self._sync_wait_task.done():
            self._sync_wait_task.cancel()
        self._sync_wait_task = self.bot.loop.create_task(delayed_sync())

    async def ensure_storage_channel(self):
        """ストレージ用チャンネルを確認・作成する"""
        all_channels = await self.guild.fetch_channels()
        channel = discord.utils.get(all_channels, name="storage")
        if not channel:
            app_info = await self.bot.application_info()
            overwrites = {
                self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                self.bot.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True),
                app_info.owner: discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=False)
            }
            channel = await self.guild.create_text_channel("storage", overwrites=overwrites, topic="アラームちゃんの活動記録")
        self.storage_channel = channel
        logger.info(f"[{self.guild.name}] Storage channel ready: #{channel.name}")

    async def upload_data_to_channel(self):
        """バックアップをアップロードする"""
        if not self.storage_channel: return
        try:
            if not os.path.exists(self.db_file): return
            files = [discord.File(self.db_file)]

            embed = discord.Embed(title="🍦 アラームちゃんのバックアップ", color=discord.Color.from_rgb(255, 240, 245))
            recent = self.get_history()
            if recent:
                log = "".join([f"{'🍅' if h.get('category')=='pomodoro' else '⏰'} `{h.get('time')}`\n" for h in reversed(recent)])
                embed.add_field(name="直近の活動記録", value=log, inline=False)
            
            # このギルドに関連するジョブだけをカウントする処理を追加（今後実装）
            job_count = len([j for j in self.bot.scheduler.get_jobs() if not j.id.startswith(('pre_', 'snooze_'))])
            embed.add_field(name="待機中の予約", value=f"`{job_count}` 件", inline=True)
            embed.set_footer(text=f"同期完了: {datetime.now(JST).strftime('%m/%d %H:%M:%S')} | {self.guild.name}")

            new_msg = await self.storage_channel.send(embed=embed, files=files, silent=True)
            
            # 5世代残して古いメッセージを削除
            count = 0
            async for m in self.storage_channel.history(limit=50):
                if m.author == self.bot.user:
                    count += 1
                    if count > 5: await m.delete()
            logger.info(f"[{self.guild.name}] Backup uploaded successfully")
        except Exception as e:
            logger.error(f"[{self.guild.name}] Upload failed: {e}")

    async def download_data_from_channel(self):
        """最新のバックアップをダウンロードする"""
        if not self.storage_channel: return
        try:
            found_db = False
            async for message in self.storage_channel.history(limit=100):
                if message.author == self.bot.user and message.attachments:
                    for attach in message.attachments:
                        if attach.filename == f"jobs_{self.guild.id}.sqlite" and not found_db:
                            await attach.save(self.db_file); found_db = True
                if found_db: break
            logger.info(f"[{self.guild.name}] Backup downloaded successfully")
        except Exception as e:
            logger.error(f"[{self.guild.name}] Download failed: {e}")

    async def grant_storage_access(self, member):
        """ユーザーに閲覧権限を付与"""
        if not self.storage_channel or not isinstance(member, discord.Member) or member.guild.id != self.guild.id: return
        try:
            await self.storage_channel.set_permissions(member, view_channel=True, read_messages=True, send_messages=False)
        except discord.NotFound:
            self.storage_channel = None # チャンネルが消えていたら参照を消す
        except Exception as e:
            logger.error(f"[{self.guild.name}] Failed to grant storage access: {e}")

class StorageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # ギルドIDをキーにしてGuildStorageを管理する辞書
        self.guild_storages: dict[int, GuildStorage] = {}
        # 古い単一インスタンス用の変数は削除

    # 全ギルドのストレージを初期化するメソッド
    async def initialize_all_guild_storages(self):
        """botが参加している全てのギルドのストレージを初期化する"""
        # 現在参加している全てのギルドを取得
        for guild in self.bot.guilds:
            await self.setup_guild_storage(guild)
        
        logger.info(f"Initialized {len(self.guild_storages)} guild storages")

    # 特定のギルドのストレージをセットアップ
    async def setup_guild_storage(self, guild: discord.Guild):
        """指定されたギルドのストレージをセットアップ"""
        if guild.id in self.guild_storages:
            return  # 既に初期化済み
        
        # GuildStorageインスタンスを作成
        guild_storage = GuildStorage(self.bot, guild)
        self.guild_storages[guild.id] = guild_storage
        
        # ストレージチャンネルを確認・作成
        await guild_storage.ensure_storage_channel()
        # バックアップからデータを復元
        await guild_storage.download_data_from_channel()

    def get_guild_storage(self, guild_id: int) -> GuildStorage | None:
        """ギルドIDから対応するGuildStorageを取得"""
        return self.guild_storages.get(guild_id)

    def cog_unload(self):
        """Cogがアンロードされる際にスケジューラーのリスナーを解除する"""
        self.bot.scheduler.remove_listener(self.on_job_change)

    def on_job_change(self, event):
        """ジョブに変更があった際に全てのギルドの同期を依頼する（ジョブにギルドIDを含めることで将来的に最適化可能）"""
        for storage in self.guild_storages.values():
            storage.request_sync()

    # 新しくサーバーに参加した際にストレージを作成
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """新しいギルドに参加した際にストレージをセットアップ"""
        logger.info(f"Joined new guild: {guild.name} (ID: {guild.id})")
        await self.setup_guild_storage(guild)

    # ギルドから退出した際にストレージをクリーンアップ
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """ギルドから削除された際にストレージをクリーンアップ"""
        if guild.id in self.guild_storages:
            del self.guild_storages[guild.id]
            logger.info(f"Removed from guild: {guild.name} (ID: {guild.id}), cleaned up storage")

    # ストレージチャンネルが削除された際の処理
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """ストレージチャンネルが削除されたら再作成する"""
        guild_id = channel.guild.id
        storage = self.get_guild_storage(guild_id)
        if storage and storage.storage_channel and channel.id == storage.storage_channel.id:
            await storage.ensure_storage_channel()
            await storage.upload_data_to_channel()

    # ストレージチャンネル内の他人のメッセージを削除
    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild:
            return
        storage = self.get_guild_storage(message.guild.id)
        if storage and storage.storage_channel and message.channel.id == storage.storage_channel.id:
            if message.author != self.bot.user and message.type == discord.MessageType.default:
                await message.delete()

    # 全てのギルドのバックアップをアップロード（bot終了時用）
    async def upload_all_data_to_channels(self):
        """全てのギルドのバックアップをアップロード"""
        upload_tasks = []
        for storage in self.guild_storages.values():
            upload_tasks.append(storage.upload_data_to_channel())
        await asyncio.gather(*upload_tasks, return_exceptions=True)

async def setup(bot):
    storage_cog = StorageCog(bot)
    await bot.add_cog(storage_cog)
    # bot.guild_storageにインスタンスを設定して他のcogから参照できるようにする
    bot.guild_storage = storage_cog
    # スケジューラーの監視設定（メソッドを直接登録）
    bot.scheduler.add_listener(storage_cog.on_job_change, EVENT_JOB_REMOVED | EVENT_JOB_EXECUTED | EVENT_JOB_ADDED | EVENT_JOB_MODIFIED)