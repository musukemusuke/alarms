from datetime import datetime, timedelta
from discord.ext import commands
from discord import app_commands
from utils import JST
from cogs.voice_cog import task_execute_pomodoro

class PomodoroCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="pomodoro", description="作業と休憩のサイクルを開始します")
    async def pomodoro(self, interaction, 
        work_mins: int = app_commands.Parameter(name="work_mins", default=25), 
        rest_mins: int = app_commands.Parameter(name="rest_mins", default=5), 
        memo: str = None):
        if self.bot.guild_storage and interaction.guild:
            guild_storage = self.bot.guild_storage.get_guild_storage(interaction.guild.id)
            if guild_storage: await guild_storage.grant_storage_access(interaction.user)
        if not interaction.user.voice: return await interaction.response.send_message("❌ VCに入ってください。", ephemeral=True)
        
        try:
            work_end = datetime.now(JST) + timedelta(minutes=work_mins)
            job_id = f"pomo_work_{interaction.user.id}_{work_end.strftime('%H%M%S')}"
            
            for j in self.bot.scheduler.get_jobs():
                if "pomo_" in j.id and str(interaction.user.id) in j.id:
                    self.bot.scheduler.remove_job(j.id)

            # 開始メッセージでも @time を反映して、何分に終わるかハッキリさせる
            display_memo = memo.replace("@time", work_end.strftime('%H:%M')) if memo else ""
            memo_info = f"「**{display_memo}**」" if display_memo else ""

            # 作業終了時のタスクを登録
            self.bot.scheduler.add_job(task_execute_pomodoro, 'date', run_date=work_end, args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, 0.5, work_mins, rest_mins, True, 0, memo], id=job_id)
            ts = int(work_end.timestamp())
            
            # 履歴はポモドーロ完了時に追加するので、開始時は記録しない
            await interaction.response.send_message(f"🍅 {memo_info}開始: {work_mins}分集中\n終了予定: <t:{ts}:t> (**<t:{ts}:R>**)", ephemeral=True)
        except:
            await interaction.response.send_message("⚠️ エラー", ephemeral=True)

async def setup(bot): await bot.add_cog(PomodoroCog(bot))