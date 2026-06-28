from datetime import datetime, timedelta
import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.jobstores.base import JobLookupError
from utils import JST, parse_days_to_cron, alarm_id_autocomplete, day_of_week_autocomplete, time_autocomplete
from cogs.voice_cog import task_execute_alarm, task_pre_notify

class AlarmCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def stop_playback(self, job_id: str):
        engine = self.bot.get_cog('VoiceCog')
        if engine: await engine.stop_playback(job_id)

    @app_commands.command(name="alarm", description="アラームをセットします")
    @app_commands.autocomplete(day_of_week=day_of_week_autocomplete)
    async def set_alarm(self, interaction: discord.Interaction, 
        time_str: str, 
        memo: str = None, 
        day_of_week: str = app_commands.Parameter(name="day_of_week", default="毎日")):
        if self.bot.guild_storage and interaction.guild:
            guild_storage = self.bot.guild_storage.get_guild_storage(interaction.guild.id)
            if guild_storage: await guild_storage.grant_storage_access(interaction.user)
        if not interaction.user.voice: return await interaction.response.send_message("❌ VCに入ってください。", ephemeral=True)

        try:
            now = datetime.now(JST)
            # 入力形式を判定：数値（相対分数） / YYYY/MM/DD HH:MM（絶対日時） / HH:MM（今日のその時刻）
            if time_str.isdigit():
                # 相対時間（数値のみ）
                target_time = now + timedelta(minutes=int(time_str))
                actual_time_str = target_time.strftime('%H:%M')
            elif '/' in time_str and ':' in time_str:
                # 絶対日時形式 "YYYY/MM/DD HH:MM"
                date_obj = datetime.strptime(time_str, "%Y/%m/%d %H:%M")
                target_time = now.replace(year=date_obj.year, month=date_obj.month, day=date_obj.day,
                                        hour=date_obj.hour, minute=date_obj.minute, second=0, microsecond=0)
                actual_time_str = date_obj.strftime('%H:%M')
            else:
                # 通常の時刻形式 "HH:MM"（今日のその時刻）
                time_obj = datetime.strptime(time_str, "%H:%M")
                target_time = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)
                actual_time_str = time_str

            time_id = target_time.strftime('%H%M')
            cron_days = parse_days_to_cron(day_of_week)
            
            for prefix in ['alarm', 'once']:
                for p in ['', 'pre_']:
                    try: self.bot.scheduler.remove_job(f"{p}{prefix}_{interaction.user.id}_{time_id}")
                    except: pass

            # day_of_weekが"一度きり"の場合は一回だけ実行、それ以外は繰り返し
            if day_of_week != "一度きり":
                # 確認メッセージ用の時刻計算（既に過ぎている場合は翌日の同じ曜日まで加算）
                confirm_time = target_time
                if confirm_time <= now:
                    confirm_time += timedelta(days=1)
                    m = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
                    target_weekdays = [m[d] for d in cron_days.split(",")] if cron_days != "*" else list(range(7))
                    while confirm_time.weekday() not in target_weekdays:
                        confirm_time += timedelta(days=1)
                ts = int(confirm_time.timestamp())
                
                # 繰り返し設定 (cron)
                job_id = f"alarm_{interaction.user.id}_{time_id}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'cron', day_of_week=cron_days, hour=target_time.hour, minute=target_time.minute,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, 0.5, actual_time_str, memo, day_of_week],
                    id=job_id
                )
                # 5分前通知の登録
                pre_time = target_time - timedelta(minutes=5)
                self.bot.scheduler.add_job(
                    task_pre_notify, 'cron', day_of_week=cron_days, hour=pre_time.hour, minute=pre_time.minute,
                    args=[interaction.channel.id, job_id, actual_time_str, memo],
                    id=f"pre_{job_id}"
                )
                description = f"指定した曜日（{day_of_week}）に繰り返します。\n次は <t:{ts}:t> (**<t:{ts}:R>**) です。"
                history_days = day_of_week
            else:
                # 一度きりの実行（指定した日付の次の時間まで進める）
                if target_time <= now:
                    target_time += timedelta(days=1)
                ts = int(target_time.timestamp())
                
                job_id = f"once_{interaction.user.id}_{time_id}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'date', run_date=target_time,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, 0.5, actual_time_str, memo, "一度きり"],
                    id=job_id
                )
                # 5分前通知
                pre_time = target_time - timedelta(minutes=5)
                if pre_time > now:
                    self.bot.scheduler.add_job(
                        task_pre_notify, 'date', run_date=pre_time,
                        args=[interaction.channel.id, job_id, actual_time_str, memo],
                        id=f"pre_{job_id}"
                    )
                description = f"🗓️ <t:{ts}:d> に一度のみ実行します。\n予定: <t:{ts}:t> (**<t:{ts}:R>**)"
                history_days = "一度きり"
            
            # 履歴はアラーム実行時に追加するので、予約時は記録しない
            await interaction.response.send_message(f"✅ アラームをセットしました。\n{description}", ephemeral=True)
        except:
            await interaction.response.send_message("⚠️ 時刻形式エラー (`HH:mm` / `YYYY/MM/DD HH:MM` / `10` のような分数を入力してください)", ephemeral=True)

    @app_commands.command(name="alarms", description="予約中の自分のアラームを表示します")
    async def list_alarms(self, interaction: discord.Interaction):
        if self.bot.guild_storage and interaction.guild:
                guild_storage = self.bot.guild_storage.get_guild_storage(interaction.guild.id)
                if guild_storage: await guild_storage.grant_storage_access(interaction.user)
        jobs = self.bot.scheduler.get_jobs()
        user_jobs = sorted([j for j in jobs if str(interaction.user.id) in j.id and not j.id.startswith('pre_')], key=lambda x: x.next_run_time)

        if not user_jobs: return await interaction.response.send_message("予約なし", ephemeral=True)

        lines = []
        for j in user_jobs:
            ts = int(j.next_run_time.timestamp())
            # ジョブの引数からメモ(memo)を抽出して表示を分かりやすくする
            # アラームなら index 6, ポモドーロなら index 9
            memo = ""
            if j.id.startswith('pomo_'):
                memo = j.args[9] if len(j.args) > 9 else "ポモドーロ"
            else:
                memo = j.args[6] if len(j.args) > 6 else "なし"
            
            lines.append(f"<t:{ts}:t> (<t:{ts}:R>) - **{memo}** (`{j.id[:8]}...`)")
        
        await interaction.response.send_message(f"⏰ **現在の予約スケジュール**\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="cancel", description="セットしたアラームを解除します")
    @app_commands.autocomplete(alarm_selection=alarm_id_autocomplete)
    async def cancel_alarm(self, interaction: discord.Interaction, 
        alarm_selection: str = app_commands.Parameter(name="alarm_selection")):
        try:
            self.bot.scheduler.remove_job(alarm_selection)
            try: self.bot.scheduler.remove_job(f"pre_{alarm_selection}")
            except: pass
            await interaction.response.send_message("🗑️ キャンセルしました。", ephemeral=True)
        except:
            await interaction.response.send_message("❌ 見つかりません。", ephemeral=True)

async def setup(bot): await bot.add_cog(AlarmCog(bot))