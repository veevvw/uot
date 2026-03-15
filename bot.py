HRB = "8771627954:AAEEtlys_HIxBlsbgALjBq6TtWJ1-0zji-E"

import os, asyncio, logging, json, time, re, shutil
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
import yt_dlp, aiofiles, requests
from asyncio_throttle import Throttler
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode, ChatAction

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DL_DIR, MAX_SIZE, MAX_CONCURRENT = Path('./downloads'), 50*1024*1024, 10
[Path(DL_DIR/d).mkdir(exist_ok=True) for d in ['', 'temp', 'thumbs']]
throttle = Throttler(rate_limit=MAX_CONCURRENT, period=0.1)
users = {}

class Downloader:
    PLATFORMS = {'youtube':['youtube.com','youtu.be'],'instagram':['instagram.com'],'tiktok':['tiktok.com','vm.tiktok.com'],
                'twitter':['twitter.com','x.com'],'facebook':['facebook.com'],'vimeo':['vimeo.com'],'dailymotion':['dailymotion.com'],
                'twitch':['twitch.tv','clips.twitch.tv'],'soundcloud':['soundcloud.com'],'reddit':['reddit.com','v.redd.it'],
                'github':['github.com'],'gitlab':['gitlab.com'],'bitbucket':['bitbucket.org'],'heroku':['git.heroku.com'],
                'codeberg':['codeberg.org'],'gitea':['gitea.com'],'sourceforge':['sourceforge.net']}
    
    def __init__(self): self.active = set()
    
    def platform(self, url): return next((p for p,d in self.PLATFORMS.items() if any(x in url.lower() for x in d)), None)
    
    async def info(self, url):
        try:
            with yt_dlp.YoutubeDL({'quiet':True,'no_warnings':True}) as ydl:
                i = ydl.extract_info(url, download=False)
                return {k:i.get(k,'N/A') for k in ['title','uploader','duration','view_count','thumbnail','filesize','ext']} if i else None
        except Exception as e:
            logger.error(f"Info extraction failed: {e}")
            return None
    
    async def thumb(self, url, cid):
        if not url: return None
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                p = DL_DIR/'thumbs'/f'{cid}.jpg'
                p.write_bytes(r.content)
                with Image.open(p) as img: img.thumbnail((320,320)); img.save(p,'JPEG',quality=85)
                return str(p)
        except: pass
        return None
    
    async def download_repo(self, url, cid, platform):
        did = f"repo_{cid}_{int(time.time())}"
        if did in self.active: return None
        self.active.add(did)
        
        try:
            original_url = url
            repo_name = "project"
            
            if platform == 'github':
                if '/tree/' in url: url = url.replace('/tree/', '/archive/refs/heads/') + '.zip'
                elif url.endswith('/'): url = url.rstrip('/') + '/archive/refs/heads/main.zip'
                else: url = url + '/archive/refs/heads/main.zip'
                repo_name = url.split('/')[-3] if len(url.split('/')) > 3 else "github-project"
                
            elif platform == 'gitlab':
                if '/-/tree/' in url: url = url.replace('/-/tree/', '/-/archive/') + '/archive.zip'
                elif url.endswith('/'): url = url.rstrip('/') + '/-/archive/main/archive.zip'
                else: url = url + '/-/archive/main/archive.zip'
                repo_name = url.split('/')[-4] if len(url.split('/')) > 4 else "gitlab-project"
                
            elif platform == 'bitbucket':
                if '/src/' in url: url = url.split('/src/')[0] + '/get/main.zip'
                elif url.endswith('/'): url = url.rstrip('/') + '/get/main.zip'
                else: url = url + '/get/main.zip'
                repo_name = url.split('/')[-3] if len(url.split('/')) > 3 else "bitbucket-project"
                
            elif platform in ['codeberg', 'gitea']:
                if '/src/' in url: url = url.replace('/src/', '/archive/') + '.zip'
                elif url.endswith('/'): url = url.rstrip('/') + '/archive/main.zip'
                else: url = url + '/archive/main.zip'
                repo_name = url.split('/')[-3] if len(url.split('/')) > 3 else f"{platform}-project"
                
            elif platform == 'sourceforge':
                if '/tree/' in url: url = url.replace('/tree/', '/tarball/')
                else: url = url + '/tarball/master'
                repo_name = url.split('/')[-3] if len(url.split('/')) > 3 else "sourceforge-project"
            
            temp = DL_DIR/'temp'/did; temp.mkdir(exist_ok=True)
            
            r = requests.get(url, timeout=30, stream=True, allow_redirects=True)
            if r.status_code != 200 and platform in ['github', 'gitlab', 'codeberg', 'gitea']:
                url = url.replace('/main.zip', '/master.zip').replace('/main/', '/master/')
                r = requests.get(url, timeout=30, stream=True, allow_redirects=True)
            
            if r.status_code == 200:
                ext = '.tar.gz' if platform == 'sourceforge' else '.zip'
                zip_path = temp / f'{repo_name}{ext}'
                
                with open(zip_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                if zip_path.stat().st_size > 0:
                    return {'file':str(zip_path), 'size':zip_path.stat().st_size, 'temp':str(temp), 'name':repo_name, 'platform':platform}
            
            return None
        except Exception as e:
            logger.error(f"{platform.title()} download failed: {e}")
            return None
        finally:
            self.active.discard(did)
    
    async def download(self, url, cid, quality='hd'):
        did = f"{cid}_{int(time.time())}"
        if did in self.active: return None
        self.active.add(did)
        
        try:
            async with throttle:
                info = await self.info(url)
                if not info: return None
                
                fmt = 'bestaudio/best' if quality == 'audio' else 'best[height<=720]/best'
                
                temp = DL_DIR/'temp'/did; temp.mkdir(exist_ok=True)
                
                opts = {'outtmpl':str(temp/'%(title)s.%(ext)s'),'format':fmt,'noplaylist':True,
                       'extractaudio':quality=='audio','audioformat':'mp3' if quality=='audio' else None}
                
                with yt_dlp.YoutubeDL(opts) as ydl: 
                    ydl.download([url])
                
                files = list(temp.glob('*'))
                video = next((f for f in files if f.suffix.lower() in ['.mp4','.mkv','.webm','.mp3','.m4a']), None)
                
                if not video: 
                    logger.error(f"No video file found in {temp}")
                    return None
                
                return {'file':str(video), 'thumb':await self.thumb(info['thumbnail'],cid), 
                       'info':info, 'size':video.stat().st_size, 'temp':str(temp)}
        except Exception as e: logger.error(f"Download failed: {e}")
        finally: self.active.discard(did)
        return None

dl = Downloader()

async def start(u, c):
    uid = u.effective_user.id
    users.setdefault(uid, {'quality':'hd','downloads':0,'last':datetime.now()})
    
    kb = [[InlineKeyboardButton(f"احصائيات", callback_data='stats'), InlineKeyboardButton(f"المنصات", callback_data='platforms')]]
    
    await u.message.reply_text(
        f"**اهلا {u.effective_user.first_name}**\n\n"
        f"**بوت التحميل الشامل**\n"
        f"**{len(dl.PLATFORMS)} منصة مدعومة**\n\n"
        f"**ارسل رابط فيديو او مشروع برمجي**",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def handle_url(u, c):
    url, cid, uid = u.message.text.strip(), u.effective_chat.id, u.effective_user.id
    
    if not any(x in url for x in ['http://', 'https://']): 
        return await u.message.reply_text("**رابط غير صحيح**", parse_mode=ParseMode.MARKDOWN)
    
    platform = dl.platform(url)
    
    if platform in ['github', 'gitlab', 'bitbucket', 'heroku', 'codeberg', 'gitea', 'sourceforge']:
        platform_names = {'github':'GitHub', 'gitlab':'GitLab', 'bitbucket':'Bitbucket', 'heroku':'Heroku', 
                         'codeberg':'Codeberg', 'gitea':'Gitea', 'sourceforge':'SourceForge'}
        
        msg = await u.message.reply_text(f"**جاري تحميل مشروع {platform_names[platform]}...**", parse_mode=ParseMode.MARKDOWN)
        try:
            result = await dl.download_repo(url, cid, platform)
            if not result:
                return await msg.edit_text("**فشل تحميل المشروع**", parse_mode=ParseMode.MARKDOWN)
            
            await msg.edit_text("**جاري رفع المشروع...**", parse_mode=ParseMode.MARKDOWN)
            
            async with aiofiles.open(result['file'], 'rb') as f:
                content = await f.read()
                ext = '.tar.gz' if platform == 'sourceforge' else '.zip'
                caption = f"**مشروع {platform_names[platform]}** | {result['name']}\n**الحجم:** {result['size']/(1024*1024):.1f}MB"
                
                await c.bot.send_document(cid, content, caption=caption, parse_mode=ParseMode.MARKDOWN,
                                        filename=f"{result['name']}{ext}")
            
            users[uid]['downloads'] += 1; users[uid]['last'] = datetime.now()
            shutil.rmtree(result['temp'], ignore_errors=True)
            try:
                await msg.delete()
            except:
                pass
            return
            
        except Exception as e:
            logger.error(f"{platform.title()} handling error: {e}")
            try:
                await msg.edit_text("**خطأ في تحميل المشروع**", parse_mode=ParseMode.MARKDOWN)
            except:
                pass
            return
    
    msg = await u.message.reply_text("**جاري التحليل...**", parse_mode=ParseMode.MARKDOWN)
    
    try:
        info = await dl.info(url)
        if not info: return await msg.edit_text("**فشل التحليل**", parse_mode=ParseMode.MARKDOWN)
        
        kb = [[InlineKeyboardButton("صوت MP3", callback_data=f'd_audio_{cid}'), InlineKeyboardButton("فيديو HD", callback_data=f'd_hd_{cid}')]]
        
        c.user_data['url'], c.user_data['info'] = url, info
        
        await msg.edit_text(
            f"**{info['title'][:50]}**\n"
            f"**{platform.title() if platform else 'Unknown'}**\n\n"
            f"**اختر نوع التحميل:**", 
            reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            
    except Exception as e:
        logger.error(f"URL handling error: {e}")
        await msg.edit_text("**خطأ في المعالجة**", parse_mode=ParseMode.MARKDOWN)

async def process_download(u, c, quality):
    q = u.callback_query; await q.answer()
    url, info = c.user_data.get('url'), c.user_data.get('info')
    if not url: return await q.edit_message_text("**انتهت الجلسة**", parse_mode=ParseMode.MARKDOWN)
    
    cid, uid = u.effective_chat.id, u.effective_user.id
    msg = await q.edit_message_text("**جاري التحميل...**", parse_mode=ParseMode.MARKDOWN)
    
    try:
        result = await dl.download(url, cid, quality)
        
        if not result: return await msg.edit_text("**فشل التحميل**", parse_mode=ParseMode.MARKDOWN)
        
        
        await msg.edit_text("**جاري الرفع...**", parse_mode=ParseMode.MARKDOWN)
        
        try:
            async with aiofiles.open(result['file'], 'rb') as f:
                content = await f.read()
                caption = f"**تم التحميل** | {result['size']/(1024*1024):.1f}MB"
                
                if quality == 'audio':
                    await c.bot.send_audio(cid, content, caption=caption, parse_mode=ParseMode.MARKDOWN,
                                         filename=Path(result['file']).name)
                else:
                    await c.bot.send_video(cid, content, caption=caption, parse_mode=ParseMode.MARKDOWN,
                                         filename=Path(result['file']).name)
        except Exception as upload_error:
            logger.error(f"Upload error: {upload_error}")
            if "too large" in str(upload_error).lower() or "file size" in str(upload_error).lower():
                return await msg.edit_text(f"**الملف كبير للتليجرام** | جرب جودة أقل", parse_mode=ParseMode.MARKDOWN)
            else:
                return await msg.edit_text(f"**خطأ في الرفع** | جرب مرة أخرى", parse_mode=ParseMode.MARKDOWN)
        
        users[uid]['downloads'] += 1; users[uid]['last'] = datetime.now()
        shutil.rmtree(result['temp'], ignore_errors=True)
        try:
            await msg.delete()
        except:
            pass
        c.user_data.clear()
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        try:
            await msg.edit_text("**خطأ في التحميل**", parse_mode=ParseMode.MARKDOWN)
        except:
            pass

async def callback_handler(u, c):
    q, data = u.callback_query, u.callback_query.data
    await q.answer()
    
    if data.startswith('d_'): 
        _, quality, cid = data.split('_', 2)
        await process_download(u, c, quality)
    elif data == 'stats': 
        s = users.get(u.effective_user.id, {})
        await q.edit_message_text(f"**احصائياتك**\n**التحميلات:** {s.get('downloads',0)}\n**اخر استخدام:** {s.get('last',datetime.now()).strftime('%Y-%m-%d')}", parse_mode=ParseMode.MARKDOWN)
    elif data == 'platforms': 
        await q.edit_message_text(f"**المنصات المدعومة:**\n" + "\n".join([f"• **{p.title()}**: {', '.join(d)}" for p,d in dl.PLATFORMS.items()]), parse_mode=ParseMode.MARKDOWN)
    elif data == 'cancel': 
        await q.edit_message_text("**تم الالغاء**", parse_mode=ParseMode.MARKDOWN); c.user_data.clear()

async def handle_text(u, c):
    text = u.message.text.lower()
    if any(w in text for w in ['مساعدة','help']): await start(u,c)
    else: await u.message.reply_text("**ارسل رابط للتحميل**\n**• فيديوهات:** YouTube, Instagram, TikTok...\n**• مشاريع:** GitHub, GitLab, Bitbucket...\n**/start للقائمة الرئيسية**", parse_mode=ParseMode.MARKDOWN)

def main():
    if not HRB: return print("No TOKEN found!")
    
    app = Application.builder().token(HRB).build()
    
    handlers = [
        CommandHandler('start', start),
        MessageHandler(filters.Regex(r'https?://'), handle_url),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
        CallbackQueryHandler(callback_handler)
    ]
    [app.add_handler(h) for h in handlers]
    
    async def error_handler(u, c): 
        logger.error(f"Error: {c.error}")
    
    app.add_error_handler(error_handler)
    
    print("UNIVERSAL DOWNLOADER BOT - ALL PLATFORMS")
    print("=" * 50)
    print(f"Total Platforms: {len(dl.PLATFORMS)}")
    print(f"Video Platforms: YouTube, Instagram, TikTok, Twitter...")
    print(f"Code Platforms: GitHub, GitLab, Bitbucket, Heroku...")
    print(f"Max Speed: {MAX_CONCURRENT} concurrent downloads")
    print("=" * 50)
    print("Universal Bot Running...")
    
    try: app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt: print("\nBot stopped")
    except Exception as e: print(f"Error: {e}")
    finally: print("Speed Bot Offline")

if __name__ == '__main__': main()
