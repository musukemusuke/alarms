import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import shutil

import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_REMOVED, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, EVENT_JOB_MODIFIED
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from dotenv import load_dotenv

# インポートを整理
from utils import JST, AUDIO_DIR

# ログの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
STORAGE_CHANNEL_ID = os.getenv('STORAGE_CHANNEL_ID') # データを保存するチャンネルID
GUILD_ID = os.getenv('GUILD_ID') # ストレージチャンネルを作成するサーバーID

# インテントの設定
intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

class AlarmBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        
        # 実行ファイルのディレクトリを取得してパスを動的に設定
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.scheduler = AsyncIOScheduler(timezone=JST)
        self.db_file = os.path.join(base_dir, "jobs.sqlite")
        self.base_dir = base_dir
        self.guild_storage = None  # storage_cogのインスタンスを格納する属性を事前に宣言

    async def setup_hook(self):
        # データの復元とエンジンの読み込み
        await self.load_extension('cogs.storage_cog')
        # storage_cogがロードされてbot.guild_storageが設定された後に全ギルドのストレージを初期化
        self.tree.on_error = self.on_app_command_error
        # ジョブストアは共通だが、将来的にギルドごとに分離することも可能
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{self.db_file}')
        }
        self.scheduler.configure(jobstores=jobstores)
        
        # storage_cogの初期化後にスケジューラーを起動
        if hasattr(self, 'guild_storage') and self.guild_storage:
            logger.info("Initializing all guild storages...")
            await self.guild_storage.initialize_all_guild_storages()
        
        # スケジューラーを起動（configure後に1回だけ実行）
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started successfully")

        # 各種機能の読み込み
        for ext in ['voice_cog', 'alarm_cog', 'pomodoro_cog', 'utility_cog']:
            await self.load_extension(f'cogs.{ext}')

        await self.tree.sync()
        logger.info("アラームちゃん 準備完了")

    @property
    def storage(self):
        return self.get_cog('StorageCog')

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """コマンド実行中にエラーが発生した際の共通処理"""
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ クールタイム中です。{error.retry_after:.1f}秒後に再度試してください。"
        else:
            logger.error(f"Unhandled command error: {error}")
            msg = "⚠️ コマンドの実行中に予期せぬエラーが発生しました。"
        
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True, silent=True)

    async def on_command_error(self, context, exception):
        """メンション等での誤反応（CommandNotFound）を無視する"""
        if isinstance(exception, commands.CommandNotFound):
            return
        logger.error(f"Prefix command error: {exception}")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user.name}")
        await self.change_presence(activity=discord.Game(name="作:m330z2_(musuke)"))

    async def close(self):
        logger.info("Bot is shutting down. Finalizing state...")
        if self.guild_storage:
            await self.guild_storage.upload_all_data_to_channels()
        if self.scheduler.running:
            self.scheduler.shutdown()
        await super().close()

bot = AlarmBot()
if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN is missing. Please set it in GitHub Secrets or .env file.")
    else:
        bot.run(TOKEN)