import os
import discord
from discord.ext import commands, tasks
import asyncio
import re
import json
import datetime

import sec.sec_save as sec_save
import core.gemini_service as gemini_service
import core.warning_service as warning_service
import sec.sec_fetch as fetch
import sec.sec_save as save
from config.db_config import get_db_connection

from config.subscriptions import (
    subscribe, unsubscribe, get_subscriptions, 
    get_all_subscriptions, get_ticker_channel, set_ticker_channel
)
from core.sec_checker import check_new_filings
from config.messages import M

class SECBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True # 메시지 내용을 읽기 위해 필요
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        # 봇이 켜질 때 슬래시 명령어 동기화
        await self.tree.sync()
        # SEC 확인 스케줄러 시작
        self.check_sec_loop.start()

    async def on_ready(self):
        print(M["LOG_BOT_READY"].format(user=self.user, user_id=self.user.id))
        print("------")

    @tasks.loop(seconds=20.0)
    async def check_sec_loop(self):
        print(M["LOG_TASK_START"])
        all_subs = get_all_subscriptions()
        
        # 중복 조회를 방지하기 위해 ticker 단위로 유저 매핑
        ticker_to_users = {}
        for user_id_str, tickers in all_subs.items():
            for t in tickers:
                ticker_to_users.setdefault(t, []).append(user_id_str)
        
        active_tickers = [t for t, users in ticker_to_users.items() if users]
        if not active_tickers:
            return

        # 병렬 처리를 위해 각 티커별 작업을 생성
        async def process_ticker(ticker):
            try:
                # check_new_filings는 동기 함수이므로 루프를 막지 않게 run_in_executor 사용 권장되나
                # 여기서는 단순화를 위해 직접 호출하거나 비동기 래핑 고려
                # 우선은 속도를 위해 병렬 실행 구조로 변경
                loop = asyncio.get_event_loop()
                new_filings = await loop.run_in_executor(None, check_new_filings, ticker)
                
                for filing in new_filings:
                    embed = discord.Embed(
                        title=M["EMBED_NEW_FILING_TITLE"].format(ticker=ticker, form_type=filing['form_type']),
                        url=filing.get("filing_html_url") or filing.get("filing_txt_url") or "https://www.sec.gov",
                        description=M["EMBED_NEW_FILING_DESC"].format(ticker=ticker, form_type=filing['form_type']),
                        color=discord.Color.blue()
                    )
                    embed.add_field(name=M["EMBED_FIELD_DATE"], value=filing.get("filing_date", M["EMBED_VALUE_NA"]), inline=True)
                    embed.add_field(name=M["EMBED_FIELD_ACC_NO"], value=filing.get("accession_no", M["EMBED_VALUE_NA"]), inline=True)
                    if filing.get("accepted_at"):
                        embed.add_field(name=M["EMBED_FIELD_ACCEPTED"], value=filing.get("accepted_at", M["EMBED_VALUE_NA"]), inline=False)

                    conn_subs = get_db_connection()
                    try:
                        with conn_subs.cursor() as cursor:
                            cursor.execute("SELECT guild_id, channel_id FROM sec_ticker_channel WHERE ticker = %s", (ticker.upper(),))
                            target_channels = cursor.fetchall()
                    finally:
                        conn_subs.close()

                    sent_any = False
                    for ch in target_channels:
                        guild_id = int(ch['guild_id'])
                        channel_id = int(ch['channel_id'])

                        guild = self.get_guild(guild_id)
                        if not guild: continue
                        channel = guild.get_channel(channel_id)
                        if not channel: continue

                        msg = await channel.send(content=M["MENTION_NEW_FILING"], embed=embed)
                        sent_any = True

                        thread_name = M["THREAD_TITLE"].format(
                            form_type=filing.get('form_type', 'Unknown'),
                            date=filing.get('filing_date', M["EMBED_VALUE_NA"])
                        )
                        thread = await msg.create_thread(name=thread_name, auto_archive_duration=1440)
                        loading_msg = await thread.send(M["THREAD_SUMMARY_LOADING"])

                        try:
                            acc_no = filing.get("accession_no", "")
                            form_type = filing.get("form_type", "")

                            text = await loop.run_in_executor(None, sec_save.get_filing_text, acc_no)
                            result_str = await loop.run_in_executor(None, gemini_service.summarize_filing, ticker, form_type, text)

                            try:
                                result_json = json.loads(result_str)
                                new_thread_name = result_json.get("thread_title", thread_name)
                                if len(new_thread_name) > 100:
                                    new_thread_name = new_thread_name[:97] + "..."
                                await thread.edit(name=new_thread_name)
                                summary_content = result_json.get("summary", "내용을 불러올 수 없습니다.")
                                await msg.edit(content=f"🔔 **{new_thread_name}**")
                            except json.JSONDecodeError:
                                summary_content = result_str

                            await loading_msg.edit(content=summary_content)
                        except Exception as e:
                            await loading_msg.edit(content=M.get("THREAD_SUMMARY_ERROR", f"Error: {e}"))

                    if sent_any:
                        await loop.run_in_executor(None, sec_save.mark_filing_notified, filing.get("accession_no", ""))
            except Exception as e:
                print(M["LOG_TASK_ERR_CHECK"].format(ticker=ticker, err=e))

        # 모든 티커를 동시에 처리
        await asyncio.gather(*(process_ticker(t) for t in active_tickers))

    @check_sec_loop.before_loop
    async def before_check_sec_loop(self):
        await self.wait_until_ready()

bot = SECBot()

@bot.event
async def on_message(message: discord.Message):
    # 봇 자신이 보낸 메시지면 무시
    if message.author.bot:
        return

    # 쓰레드에서 온 메시지인지 확인
    if isinstance(message.channel, discord.Thread) and message.channel.category and message.channel.category.name == M["CATEGORY_NAME"]:
        loading_msg = await message.channel.send(M.get("THREAD_QA_LOADING", "⏳ 답변을 생성 중입니다..."))
        
        try:
            starter_msg = await message.channel.parent.fetch_message(message.channel.id)
            ticker = message.channel.parent.name.upper()
            
            embed = starter_msg.embeds[0] if starter_msg.embeds else None
            if not embed:
                await loading_msg.edit(content=M["THREAD_QA_FETCH_ERR"])
                return
                
            m = re.search(r'\((.*?)\)$', embed.title)
            form_type = m.group(1) if m else "Unknown"
            
            accession_no = ""
            for field in embed.fields:
                if field.name == M["EMBED_FIELD_ACC_NO"]:
                    accession_no = field.value
                    break
                    
            text = await asyncio.get_event_loop().run_in_executor(None, sec_save.get_filing_text, accession_no)
            
            history = []
            async for msg in message.channel.history(limit=50, oldest_first=True):
                if msg.id == starter_msg.id or msg.id == loading_msg.id:
                    continue
                role = "model" if msg.author.bot else "user"
                if not msg.content: continue
                history.append({"role": role, "parts": msg.content})
                
            answer_str = await asyncio.get_event_loop().run_in_executor(
                None, 
                gemini_service.answer_question, 
                ticker, form_type, text, history, message.content
            )
            
            try:
                answer_json = json.loads(answer_str)
                is_related = answer_json.get("is_related", True)
                answer_content = answer_json.get("answer", M["THREAD_QA_ANSWER_ERR"])
                
                if not is_related:
                    warning_count = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        warning_service.add_warning, 
                        message.channel.id, message.author.id
                    )
                    
                    warn_msg = ""
                    if warning_count >= 3:
                        try:
                            timeout_reason = f"공시 및 {ticker} 주식과 무관한 질문 3회 누적"
                            await message.author.timeout(datetime.timedelta(minutes=5), reason=timeout_reason)
                            
                            # DB에 타임아웃 이력 기록
                            await asyncio.get_event_loop().run_in_executor(
                                None, 
                                warning_service.log_timeout, 
                                message.author.id, message.guild.id, timeout_reason, 5
                            )
                            warn_msg = M["THREAD_QA_WARN_TIMEOUT"].format(mention=message.author.name, ticker=ticker)
                        except discord.Forbidden:
                            warn_msg = M["THREAD_QA_WARN_NO_PERM"].format(mention=message.author.name, ticker=ticker)
                        
                        # 타임아웃 적용 후 초기화
                        await asyncio.get_event_loop().run_in_executor(
                            None, 
                            warning_service.reset_warnings, 
                            message.channel.id, message.author.id
                        )
                    else:
                        warn_msg = M["THREAD_QA_WARN"].format(mention=message.author.name, ticker=ticker, warning_count=warning_count, answer_content=answer_content)
                    
                    # 유저에게만 보이도록 DM으로 경고 전송
                    try:
                        await message.author.send(warn_msg)
                    except discord.Forbidden:
                        pass # 유저가 DM을 막아둔 경우 무시
                    
                    # 봇의 로딩 메시지는 삭제
                    try:
                        await loading_msg.delete()
                    except discord.NotFound:
                        pass
                        
                    # 5초 뒤에 유저의 엉뚱한 질문 메시지 삭제
                    try:
                        await message.delete(delay=5.0)
                    except discord.Forbidden:
                        pass
                else:
                    await loading_msg.edit(content=answer_content)
            except json.JSONDecodeError:
                await loading_msg.edit(content=answer_str)
        except Exception as e:
            await loading_msg.edit(content=M["THREAD_QA_GENERAL_ERR"].format(err=e))
        return

    # 관리자 채팅은 지우지 않음 (채널이더라도)
    if hasattr(message.author, 'guild_permissions') and message.author.guild_permissions.administrator:
        await bot.process_commands(message)
        return

    # 채널의 카테고리가 "알림"인지 확인 (티커 방들)
    if not isinstance(message.channel, discord.Thread) and message.channel.category and message.channel.category.name == M["CATEGORY_NAME"]:
        try:
            # 유저의 채팅 메시지 삭제
            await message.delete()
            # 경고 메시지를 잠시 띄웠다가 5초 뒤에 자동 삭제
            await message.channel.send(M["CMD_WARN_CHAT_DISABLED"].format(mention=message.author.mention), delete_after=5.0)
        except discord.Forbidden:
            pass # 봇에게 메시지 삭제/전송 권한이 없는 경우 무시
        except discord.NotFound:
            pass # 이미 삭제된 메시지인 경우 무시
    
    # 봇의 기본 명령어 처리 로직 유지
    await bot.process_commands(message)

async def get_or_create_ticker_channel(guild: discord.Guild, ticker: str):
    ticker = ticker.upper()
    channel_id = get_ticker_channel(ticker, str(guild.id))
    
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            return channel
            
    # 카테고리 생성 또는 가져오기 ("알림"으로 변경)
    category_name = M["CATEGORY_NAME"]
    category = discord.utils.get(guild.categories, name=category_name)
    if not category:
        category = await guild.create_category(category_name)
        
    # 권한 설정: 기본적으로 아무도 못 봄, 봇은 볼 수 있고 메시지도 쓸 수 있음
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            read_messages=False,
            send_messages=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=True
        ),
        guild.me: discord.PermissionOverwrite(
            read_messages=True, 
            send_messages=True, 
            manage_messages=True, 
            embed_links=True
        )
    }
    
    # 디스코드 텍스트 채널은 시스템상 무조건 "소문자"만 지원하므로 소문자로 생성됩니다.
    # 대문자 입력 시 에러가 발생하므로 lower()를 유지해야 합니다.
    channel_name = ticker.lower()
    channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
    
    set_ticker_channel(ticker, str(guild.id), str(channel.id))
    return channel

@bot.tree.command(name="구독", description="특정 종목의 SEC 공시 알림을 구독합니다.")
async def subscribe_cmd(interaction: discord.Interaction, ticker: str):
    ticker = ticker.upper().strip()
    
    # 티커 유효성 검사 (stocks 테이블에 존재하는지 확인)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM stocks WHERE ticker = %s", (ticker,))
            if not cursor.fetchone():
                await interaction.response.send_message(M["CMD_ERR_INVALID_TICKER"].format(ticker=ticker), ephemeral=True)
                return
    finally:
        conn.close()

    success = subscribe(str(interaction.user.id), ticker)
    
    if success:
        # 티커 채널 생성 혹은 가져오기
        channel = await get_or_create_ticker_channel(interaction.guild, ticker)
        
        # 해당 유저에게 채널 읽기 권한 부여, 쓰기 권한은 없음
        await channel.set_permissions(
            interaction.user, 
            read_messages=True, 
            send_messages=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=True
        )
        
        await interaction.response.send_message(M["CMD_SUB_SUCCESS"].format(ticker=ticker, channel_mention=channel.mention), ephemeral=True)
    else:
        # 이미 구독 중이어도 채널 권한 혹시 모르니 다시 세팅
        channel = await get_or_create_ticker_channel(interaction.guild, ticker)
        await channel.set_permissions(interaction.user, read_messages=True, send_messages=False)
        
        await interaction.response.send_message(M["CMD_SUB_ALREADY"].format(ticker=ticker, channel_mention=channel.mention), ephemeral=True)

@bot.tree.command(name="구독취소", description="특정 종목의 SEC 공시 알림 구독을 취소합니다.")
async def unsubscribe_cmd(interaction: discord.Interaction, ticker: str):
    ticker = ticker.upper()
    success = unsubscribe(str(interaction.user.id), ticker)
    
    if success:
        channel_id = get_ticker_channel(ticker, str(interaction.guild.id))
        if channel_id:
            channel = interaction.guild.get_channel(int(channel_id))
            if channel:
                # 유저의 해당 채널 권한 덮어쓰기 삭제 (채널에서 안 보이게 됨)
                await channel.set_permissions(interaction.user, overwrite=None)
                
        await interaction.response.send_message(M["CMD_UNSUB_SUCCESS"].format(ticker=ticker), ephemeral=True)
    else:
        await interaction.response.send_message(M["CMD_UNSUB_NOT_SUBBED"].format(ticker=ticker), ephemeral=True)

@bot.tree.command(name="목록", description="현재 구독 중인 종목 목록을 확인합니다.")
async def list_cmd(interaction: discord.Interaction):
    tickers = get_subscriptions(str(interaction.user.id))
    if tickers:
        ticker_mentions = []
        for t in tickers:
            ch_id = get_ticker_channel(t, str(interaction.guild.id))
            if ch_id:
                ticker_mentions.append(f"<#{ch_id}>")
            else:
                ticker_mentions.append(t)
        
        ticker_list = ", ".join(ticker_mentions)
        await interaction.response.send_message(M["CMD_LIST_RESULT"].format(ticker_list=ticker_list), ephemeral=True)
    else:
        await interaction.response.send_message(M["CMD_LIST_EMPTY"], ephemeral=True)

@bot.tree.command(name="테스트공시", description="(관리자 전용) 특정 종목의 가장 최근 실제 공시를 가져와 테스트 알림 및 요약을 전송합니다.")
@discord.app_commands.default_permissions(administrator=True)
async def test_filing_cmd(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(ephemeral=True)
    ticker = ticker.upper()
    channel_id = get_ticker_channel(ticker, str(interaction.guild.id))
    
    if not channel_id:
        await interaction.followup.send(M["CMD_TEST_NO_ROOM"].format(ticker=ticker), ephemeral=True)
        return
        
    channel = interaction.guild.get_channel(int(channel_id))
    if not channel:
        await interaction.followup.send(M["CMD_TEST_NO_CHANNEL"].format(ticker=ticker), ephemeral=True)
        return
    
    try:
        submissions = await asyncio.get_event_loop().run_in_executor(None, fetch.get_sec_submissions, ticker)
        recent = submissions.get("filings", {}).get("recent", {})
        if not recent or not recent.get("accessionNumber"):
            await interaction.followup.send(M["CMD_TEST_NO_RECENT"].format(ticker=ticker), ephemeral=True)
            return
            
        accession_no = recent["accessionNumber"][0]
        form_type = recent["form"][0]
        filing_date = recent["filingDate"][0]
        primary_doc = recent["primaryDocument"][0] if recent.get("primaryDocument") else ""
        
        cik = str(submissions.get("cik", "")).lstrip("0")
        acc_no_clean = accession_no.replace("-", "")
        base_dir = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_clean}"
        url = f"{base_dir}/{primary_doc}" if primary_doc else f"{base_dir}/{acc_no_clean}.txt"

        embed = discord.Embed(
            title=M["CMD_TEST_EMBED_TITLE"].format(ticker=ticker).replace("8-K", form_type),
            url=url,
            description=M["CMD_TEST_EMBED_DESC"].format(ticker=ticker).replace("8-K", form_type),
            color=discord.Color.red()
        )
        embed.add_field(name=M["EMBED_FIELD_DATE"], value=filing_date, inline=True)
        embed.add_field(name=M["EMBED_FIELD_ACC_NO"], value=accession_no, inline=True)
        
        msg = await channel.send(content=M["CMD_TEST_MENTION"], embed=embed)
        
        thread_name = M["THREAD_TITLE"].format(form_type=form_type, date=filing_date)
        thread = await msg.create_thread(name=thread_name, auto_archive_duration=1440)
        loading_msg = await thread.send(M["THREAD_SUMMARY_LOADING"])

        # Fetch and save text
        text = await asyncio.get_event_loop().run_in_executor(None, save.get_filing_text, accession_no)
        if not text:
            text = await asyncio.get_event_loop().run_in_executor(None, fetch.get_filing_detail, submissions, accession_no)
            await asyncio.get_event_loop().run_in_executor(None, save.save_filing_text, accession_no, text)
            
        # Summarize
        result_str = await asyncio.get_event_loop().run_in_executor(None, gemini_service.summarize_filing, ticker, form_type, text)
        
        try:
            result_json = json.loads(result_str)
            new_thread_name = result_json.get("thread_title", thread_name)
            if len(new_thread_name) > 100:
                new_thread_name = new_thread_name[:97] + "..."
            await thread.edit(name=new_thread_name)
            summary_content = result_json.get("summary", "내용을 불러올 수 없습니다.")
            # 알림 메시지 내용을 AI가 생성한 제목으로 변경
            await msg.edit(content=f"🔔 **{new_thread_name}**")
        except json.JSONDecodeError:
            summary_content = result_str
        
        await loading_msg.edit(content=summary_content)
        await interaction.followup.send(M["CMD_TEST_SUCCESS"].format(ticker=ticker), ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(M["CMD_TEST_ERR"].format(err=e), ephemeral=True)

@bot.tree.command(name="유저조회", description="(관리자 전용) 특정 유저의 경고 및 타임아웃 기록을 조회합니다.")
@discord.app_commands.default_permissions(administrator=True)
async def user_info_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        # 1. 활성 경고 가져오기
        warnings = await asyncio.get_event_loop().run_in_executor(
            None, warning_service.get_user_warnings, user.id
        )
        
        # 2. 타임아웃 로그 가져오기
        timeout_logs = await asyncio.get_event_loop().run_in_executor(
            None, warning_service.get_user_timeout_logs, user.id, interaction.guild.id
        )
        
        embed = discord.Embed(
            title=M["CMD_USER_INFO_TITLE"],
            description=M["CMD_USER_INFO_DESC"].format(user_name=user.display_name),
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url=user.display_avatar.url if user.display_avatar else None)
        
        # 경고 내역 처리
        total_warns = sum(w['warning_count'] for w in warnings)
        warn_text = ""
        if warnings:
            for w in warnings:
                date_str = w['updated_at'].strftime("%Y-%m-%d %H:%M")
                warn_text += M["CMD_USER_INFO_WARN_ITEM"].format(thread_id=w['thread_id'], count=w['warning_count'], date=date_str) + "\n"
        else:
            warn_text = M["CMD_USER_INFO_WARN_EMPTY"]
            
        embed.add_field(name=M["CMD_USER_INFO_WARN_FIELD"].format(total_warns=total_warns), value=warn_text, inline=False)
        
        # 타임아웃 로그 처리
        timeout_text = ""
        if timeout_logs:
            for t in timeout_logs:
                date_str = t['created_at'].strftime("%Y-%m-%d %H:%M")
                timeout_text += M["CMD_USER_INFO_TIMEOUT_ITEM"].format(date=date_str, reason=t['reason'], duration=t['duration_minutes']) + "\n"
        else:
            timeout_text = M["CMD_USER_INFO_TIMEOUT_EMPTY"]
            
        embed.add_field(name=M["CMD_USER_INFO_TIMEOUT_FIELD"], value=timeout_text, inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(M["CMD_USER_INFO_ERR"].format(err=e), ephemeral=True)
