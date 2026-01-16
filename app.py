import os
import json
import random
import string
from datetime import datetime, timedelta, timezone
from functools import wraps
import requests
import threading
import redis  # 导入 redis
import psutil  # [新增] 用于监控服务器状态
from collections import Counter
from flask_session import Session  # 导入 Session 扩展
from zhdate import ZhDate
# 引入 ProxyFix 修复云端/Nginx反代环境下的 Scheme 问题
from werkzeug.middleware.proxy_fix import ProxyFix
# 引入 Flask 相关组件
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
# 需要导入 Response 和 stream_with_context (Flask原生支持流式)
from flask import Response, stream_with_context
# 引入 CSRF 保护
from flask_wtf.csrf import CSRFProtect, generate_csrf
# Supabase 客户端
from supabase import create_client, Client
# 环境变量加载
from dotenv import load_dotenv
# 文件名安全处理
from werkzeug.utils import secure_filename
# [修改] 多导入一个 generate_csrf
from flask_wtf.csrf import CSRFProtect, generate_csrf
from cryptography.fernet import Fernet

LAB_CODE = "testuser8888"
# 加载 .env 文件
load_dotenv()

app = Flask(__name__)


@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')


@app.before_request
def gatekeeper():
    # 1. 白名单：静态资源、门禁页接口、PWA相关文件
    # [关键] 加上 sw.js 和 manifest.json，确保 PWA 安装不受影响
    if request.endpoint in ['static', 'lab_entry', 'verify_lab_entry'] or request.path in ['/sw.js',
                                                                                           '/static/manifest.json']:
        return

    # 2. 检查通行证 (Cookie)
    if request.cookies.get('lab_pass') != 'granted':
        return redirect(url_for('lab_entry'))


@app.route('/lab_entry')
def lab_entry():
    # [修复] 生成 CSRF Token
    token = generate_csrf()

    return f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
        <title>System Access</title>
        <style>
            body {{ background: #000; color: #00ff00; font-family: 'Courier New', monospace; display: flex; height: 100vh; justify-content: center; align-items: center; margin: 0; flex-direction: column; }}
            input {{ border: 1px solid #00ff00; background: transparent; color: #00ff00; padding: 10px; outline: none; text-align: center; font-size: 20px; letter-spacing: 5px; width: 200px; }}
            button {{ margin-top: 20px; border: 1px solid #00ff00; background: #00ff00; color: #000; padding: 10px 40px; font-weight: bold; cursor: pointer; }}
        </style>
    </head>
    <body>
        <div style="font-size: 40px; margin-bottom: 20px;">🔒</div>
        <form action="/verify_lab_entry" method="POST">
            
            <input type="tel" name="code" placeholder="CODE" autofocus>
            <br>
            <button>UNLOCK</button>
        </form>
    </body>
    </html>
    '''


@app.route('/verify_lab_entry', methods=['POST'])
def verify_lab_entry():
    if request.form.get('code') == LAB_CODE:
        resp = redirect(url_for('login'))
        # [核心] 设置 10 年有效期的 Cookie
        resp.set_cookie('lab_pass', 'granted', max_age=60 * 60 * 24 * 365 * 10, httponly=True)
        return resp
    else:
        return "<body style='background:#000;color:red;text-align:center;padding-top:50px;'><h1>ACCESS DENIED</h1><a href='/lab_entry' style='color:#fff'>RETRY</a></body>"


CURRENT_APP_VERSION = '4.1.0'
qweather_key = os.environ.get("QWEATHER_KEY")
qweather_host = os.environ.get("QWEATHER_HOST", "https://devapi.qweather.com")
ENABLE_GOD_MODE = False

# ================= 配置区域 =================
# 适配 Vercel/Render 等代理环境，防止 HTTPS 变 HTTP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Secret Key 必须设置
app.secret_key = os.environ.get("SECRET_KEY", "dev_key_must_change_to_something_complex")

# Session 有效期 30 天
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
# [新增] CSRF Token 有效期设为 None (跟随 Session，不单独过期)
app.config['WTF_CSRF_TIME_LIMIT'] = None
# 限制上传文件最大为 16MB
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# ---------------------------------------------------------
# [智能环境判断]
# 只要设置了 FLASK_ENV=production 或者在 Vercel 环境，就视为生产环境
is_production = os.environ.get('FLASK_ENV') == 'production' or os.environ.get('VERCEL') == '1'

if is_production:
    print("🚀 生产环境 (阿里云/Vercel): 启用 Redis & HTTPS 安全策略")
    # 1. Cookie 安全配置 (HTTPS)
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        WTF_CSRF_SSL_STRICT=False
    )
    # 2. Redis Session 配置
    app.config['SESSION_TYPE'] = 'redis'
    app.config['SESSION_PERMANENT'] = True
    app.config['SESSION_USE_SIGNER'] = True
    app.config['SESSION_KEY_PREFIX'] = 'family:'
    # 服务器上 Redis 就在本地，直接连
    app.config['SESSION_REDIS'] = redis.from_url('redis://127.0.0.1:6379')

else:
    print("💻 本地开发环境: 使用文件系统存储 & HTTP")
    # 1. Cookie 安全配置 (HTTP)
    app.config.update(
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax'
    )
    # 2. 文件系统 Session 配置 (无需安装 Redis)
    app.config['SESSION_TYPE'] = 'filesystem'
    app.config['SESSION_FILE_DIR'] = './flask_session_data'  # 在当前目录下生成文件夹存 Session
    app.config['SESSION_PERMANENT'] = True
# ---------------------------------------------------------

# 初始化 Session (必须在配置之后)
Session(app)

# 初始化 CSRF 保护
csrf = CSRFProtect(app)

# Supabase 配置读取
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
service_key = os.environ.get("SUPABASE_SERVICE_KEY")

if not url or not key:
    print("❌ 严重警告: 未检测到 Supabase URL 或 KEY 配置")

# ================= 客户端初始化 =================
# 1. 普通客户端 (匿名/全局，用于公开读取或登录动作)
supabase: Client = create_client(url, key)

# 2. 管理员客户端 (Service Key，拥有上帝权限，用于后台管理和代登录)
admin_supabase: Client = create_client(url, service_key) if service_key else None


# ================= 辅助函数 =================

def get_beijing_time():
    """获取当前的北京时间"""
    utc_dt = datetime.now(timezone.utc)
    return utc_dt.astimezone(timezone(timedelta(hours=8)))


def format_time_friendly(iso_str):
    """
    将 ISO 时间字符串格式化为友好的显示格式
    例如：刚刚、5分钟前、10-24 12:00
    """
    if not iso_str: return ""
    try:
        # 处理 Supabase 可能返回的 Z 结尾
        if iso_str.endswith('Z'):
            dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        else:
            dt = datetime.fromisoformat(iso_str)

        now = datetime.now(timezone.utc)
        diff = now - dt
        local_dt = dt.astimezone(timezone(timedelta(hours=8)))

        # 如果大于24小时，显示日期
        if diff.days > 0:
            return local_dt.strftime('%m-%d %H:%M')
        # 如果小于1小时
        elif diff.seconds < 3600:
            mins = diff.seconds // 60
            if mins == 0:
                return "刚刚"
            return f"{mins}分钟前"
        # 如果小于24小时
        else:
            return f"{diff.seconds // 3600}小时前"
    except:
        return iso_str[:10]


def resolve_account(input_str):
    """智能识别账号格式，自动补全邮箱后缀"""
    if not input_str: return ""
    input_str = input_str.strip()
    # 如果用户输入了包含 @ 的完整邮箱，直接使用
    if '@' in input_str:
        return input_str
    # 否则默认加上 .paw 后缀 (你可以改为你的自定义后缀)
    else:
        return f"{input_str}@family.com"


def generate_invite_code():
    """生成6位大写字母+数字的随机邀请码"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ================= 天气服务核心逻辑 =================

def search_city_qweather(keyword):
    """
    [GeoAPI] 搜索城市 ID (新版)
    URL 结构: https://你的Host/geo/v2/city/lookup
    """
    if not keyword or not qweather_key: return None, None, None, None

    # 使用配置里的 Host (比如 https://devapi.qweather.com 或你的专属域名)
    host = qweather_host
    url = f"{host.rstrip('/')}/geo/v2/city/lookup"
    try:
        params = {"location": keyword, "key": qweather_key, "range": "cn"}
        res = requests.get(url, params=params, timeout=5)
        data = res.json()

        if data.get('code') == '200' and data.get('location'):
            top = data['location'][0]
            # [关键修改] 同时返回 ID, Name, Lat, Lon
            return top['id'], top['name'], top['lat'], top['lon']

    except Exception as e:
        print(f"GeoAPI Error: {e}")

    return None, None, None, None


def get_weather_full(city_id, lat=None, lon=None):
    """
    [全能天气查询 - 最终修正版]
    1. 实时天气 (v7/weather/now)
    2. 生活指数 (v7/indices/1d) -> type=3 是穿衣指数，不是3天
    3. 空气质量 (新版 v1) -> 适配无 code 返回结构
    """
    if not city_id or not qweather_key: return None

    weather_data = {}
    # 获取配置的 Host，去除末尾斜杠
    host = os.environ.get("QWEATHER_HOST", "https://devapi.qweather.com").rstrip('/')

    try:
        # ================= 1. 实时天气 (v7) =================
        # 依然用 ID 查，最准
        url_now = f"{host}/v7/weather/now"
        res_now = requests.get(url_now, params={"location": city_id, "key": qweather_key}, timeout=3)
        data_now = res_now.json()

        if data_now.get('code') == '200':
            weather_data['now'] = data_now['now']
        else:
            return None  # 基础天气都没有，直接退出

        # ================= 2. 生活指数 (v7) =================
        # type=3: 穿衣指数, type=9: 感冒指数. endpoint是 1d (1天预报)
        url_ind = f"{host}/v7/indices/1d"
        res_ind = requests.get(url_ind, params={"type": "3,9", "location": city_id, "key": qweather_key}, timeout=3)
        data_ind = res_ind.json()

        if data_ind.get('code') == '200':
            # daily 是一个列表，转字典方便前端取
            weather_data['indices'] = {item['type']: item for item in data_ind['daily']}

        # ================= 3. 空气质量 (新版 v1) =================
        if lat and lon:
            # [修正] 强制保留2位小数
            lat_fmt = "{:.2f}".format(float(lat))
            lon_fmt = "{:.2f}".format(float(lon))

            # 拼接 URL
            url_air = f"{host}/airquality/v1/current/{lat_fmt}/{lon_fmt}"

            # 发送请求
            res_air = requests.get(url_air, params={"key": qweather_key}, timeout=3)
            data_air = res_air.json()

            # [核心修复] 新版 API 不返回 code:200，而是直接返回 indexes 列表
            # 只要 indexes 存在且不为空，就算成功
            if 'indexes' in data_air and len(data_air['indexes']) > 0:
                # 提取 AQI 类别 (优/良)
                # 构造一个和旧版结构类似的字典，方便前端兼容
                weather_data['air'] = {
                    'category': data_air['indexes'][0]['category'],
                    'aqi': data_air['indexes'][0]['aqi']
                }
            else:
                print(f"Air API No Data: {data_air}")

    except Exception as e:
        print(f"Weather Fetch Exception: {e}")
        if not weather_data.get('now'): return None

    return weather_data


def calculate_age(birthday):
    """根据生日计算 'X岁Y个月'"""
    if not birthday: return "年龄未知"
    try:
        birth = datetime.strptime(birthday, '%Y-%m-%d').date()
        today = datetime.now(timezone(timedelta(hours=8))).date()

        # 还没出生?
        if birth > today: return "即将出生"

        years = today.year - birth.year
        months = today.month - birth.month
        if today.day < birth.day: months -= 1

        if months < 0:
            years -= 1
            months += 12

        if years == 0 and months == 0:
            days = (today - birth).days
            return f"{days}天大"
        elif years == 0:
            return f"{months}个月大"
        else:
            return f"{years}岁 {months}个月"
    except:
        return "年龄未知"


# [新增] 计算事件详情
def calculate_event_details(event):
    """
    返回: {days: 剩余天数, total: 累计天数, date_str: 下次日期, is_repeat: bool}
    """
    try:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        start_date = datetime.strptime(event['event_date'], '%Y-%m-%d').date()

        # 1. 计算累计天数 (如果开始时间在过去)
        total_days = 0
        if start_date <= today:
            total_days = (today - start_date).days

        # 2. 计算下一次日期
        next_date = None

        if not event.get('is_repeat'):
            # A. 一次性事件 (如考研)
            next_date = start_date
        else:
            # B. 重复事件 (农历/公历)
            if event['event_type'] == 'lunar':
                try:
                    # 尝试今年的农历
                    lunar_next = ZhDate(today.year, start_date.month, start_date.day)
                    solar_next = lunar_next.to_datetime().date()
                    if solar_next < today:
                        # 今年过了算明年
                        lunar_next = ZhDate(today.year + 1, start_date.month, start_date.day)
                        solar_next = lunar_next.to_datetime().date()
                    next_date = solar_next
                except:
                    # 简单回退到公历防止报错
                    next_date = start_date.replace(year=today.year)
            else:
                # 公历
                try:
                    next_date = start_date.replace(year=today.year)
                except ValueError:
                    next_date = start_date.replace(year=today.year, day=28)  # 闰年修正

                if next_date < today:
                    try:
                        next_date = start_date.replace(year=today.year + 1)
                    except:
                        next_date = start_date.replace(year=today.year + 1, day=28)

        # 3. 计算剩余天数
        days_left = (next_date - today).days

        return {
            'days': days_left,
            'total': total_days,
            'date_str': next_date.strftime('%Y-%m-%d'),
            'is_repeat': event.get('is_repeat')
        }
    except Exception as e:
        print(f"Calc Error: {e}")
        return None


# [新增] 通用统计函数 (根据时间范围算出谁是冠军)
def calculate_champion(client, family_id, start_time, end_time):
    # 1. 获取成员
    mems = client.table('family_members').select('user_id').eq('family_id', family_id).execute()
    user_ids = [m['user_id'] for m in (mems.data or [])]
    if not user_ids: return None

    # 2. 初始化计数
    stats = {uid: {'guardian': 0, 'recorder': 0, 'foodie': 0, 'care': 0} for uid in user_ids}

    # 3. 统计各项数据 (带时间范围)
    # A. 守护
    pets = client.table('pets').select('id').eq('family_id', family_id).execute()
    pet_ids = [p['id'] for p in (pets.data or [])]
    if pet_ids:
        logs = client.table('logs').select('user_id').in_('pet_id', pet_ids).gte('created_at', start_time).lt(
            'created_at', end_time).execute()
        for l in (logs.data or []):
            if l['user_id'] in stats: stats[l['user_id']]['guardian'] += 1

    # B. 记录 (简化版：只查moments)
    moms = client.table('moments').select('user_id').or_(
        f"target_family_id.is.null,target_family_id.eq.{family_id}").gte('created_at', start_time).lt('created_at',
                                                                                                      end_time).execute()
    for m in (moms.data or []):
        if m['user_id'] in stats: stats[m['user_id']]['recorder'] += 1

    # C. 美食
    wishes = client.table('family_wishes').select('created_by').eq('family_id', family_id).gte('created_at',
                                                                                               start_time).lt(
        'created_at', end_time).execute()
    for w in (wishes.data or []):
        if w['created_by'] in stats: stats[w['created_by']]['foodie'] += 1

    # D. 关怀
    rems = client.table('family_reminders').select('created_by').eq('family_id', family_id).gte('created_at',
                                                                                                start_time).lt(
        'created_at', end_time).execute()
    for r in (rems.data or []):
        if r['created_by'] in stats: stats[r['created_by']]['care'] += 1

    # 4. 评选 MVP
    best_uid = None
    best_score = -1
    best_title = ""

    for uid, s in stats.items():
        # 简单加权总分 (元老值不参与周榜竞赛，只看谁干活多)
        total = s['guardian'] + s['recorder'] + s['foodie'] + s['care']

        if total > best_score and total > 0:  # 必须有贡献
            best_score = total
            best_uid = uid

            # [修改] 统一使用你指定的称号文案
            # 这样归档到历史表里的就是"金牌铲屎官"了
            scores = {
                '🛡️ 金牌铲屎官': s['guardian'],
                '📸 朋友圈战神': s['recorder'],
                '😋 干饭王': s['foodie'],
                '❤️ 贴心小棉袄': s['care']
            }
            # 直接取 Key 作为标题
            best_arr = max(scores, key=scores.get)
            best_title=f"周榜·{best_arr}"

    if best_uid:
        return {'uid': best_uid, 'title': best_title, 'score': best_score}
    return None
# ================= 数据加密模块 =================
crypto_key = os.environ.get("CRYPTO_KEY")
cipher = Fernet(crypto_key) if crypto_key else None

def encrypt_data(text):
    """加密: 明文 -> 乱码"""
    if not cipher or not text: return text
    try:
        return cipher.encrypt(text.encode()).decode()
    except: return text

def decrypt_data(text):
    """解密: 乱码 -> 明文"""
    if not cipher or not text: return text
    try:
        return cipher.decrypt(text.encode()).decode()
    except:
        # 如果解密失败(可能是旧数据是明文)，直接返回原样
        return text
# ================= 微信推送服务 (WxPusher) =================

# 从环境变量读取配置 (也可以直接填字符串)
wx_app_token = os.environ.get("WX_APP_TOKEN")
wx_topic_id = os.environ.get("WX_TOPIC_ID")


def send_wechat_push(family_id, summary, content):
    """
    [平台版] 微信推送
    family_id: 目标家庭 ID
    """
    if not wx_app_token or not family_id: return

    def _do_push():
        try:
            # 1. 既然是给家庭发，先找出这个家庭里的所有成员
            # 这里需要管理员权限(admin_supabase)或者确保 RLS 允许读取成员的 profile
            # 为了稳妥，我们用 get_db()，依赖 "同家庭可见" 的 RLS 策略
            # 注意：这需要确保当前操作者属于该家庭，或者是系统自动触发

            client = admin_supabase if admin_supabase else supabase

            # A. 查出家庭成员 ID
            mems = client.table('family_members').select('user_id').eq('family_id', family_id).execute()
            user_ids = [m['user_id'] for m in mems.data] if mems.data else []

            if not user_ids: return

            # B. 查出这些成员的 wx_uid
            # 过滤掉没有填 UID 的人
            profiles = client.table('profiles').select('wx_uid').in_('id', user_ids).neq('wx_uid', 'null').execute()
            uids = [p['wx_uid'] for p in profiles.data if p.get('wx_uid')]

            if not uids:
                print("该家庭无人绑定微信 UID，跳过推送")
                return

            # 2. 发送请求 (uids 列表)
            url = "https://wxpusher.zjiecode.com/api/send/message"
            payload = {
                "appToken": wx_app_token,
                "content": content,
                "summary": summary,
                "contentType": 1,
                "uids": uids  # [修改] 这里变成了 uids 数组
            }
            requests.post(url, json=payload, timeout=5)
            print(f"✅ 推送成功，接收人数: {len(uids)}")

        except Exception as e:
            print(f"Push Error: {e}")

    threading.Thread(target=_do_push).start()

def send_private_wechat_push(target_user_id, summary, content):
    """
    [新增] 点对点私密推送
    只发给指定用户，不打扰全家
    """
    if not wx_app_token or not target_user_id: return

    def _do_push():
        try:
            # 1. 查这个人的 Wx UID
            # 这里用 admin 权限查，确保能查到
            client = admin_supabase if admin_supabase else supabase
            res = client.table('profiles').select('wx_uid').eq('id', target_user_id).single().execute()

            if res.data and res.data.get('wx_uid'):
                uids = [res.data['wx_uid']]

                # 2. 发送
                url = "https://wxpusher.zjiecode.com/api/send/message"
                payload = {
                    "appToken": wx_app_token,
                    "content": content,
                    "summary": summary,
                    "contentType": 1,
                    "uids": uids  # 只发给他一个人
                }
                requests.post(url, json=payload, timeout=5)
                print(f"✅ 私密推送成功: {uids}")
            else:
                print("❌ 目标用户未绑定微信 UID")

        except Exception as e:
            print(f"Private Push Error: {e}")

    threading.Thread(target=_do_push).start()

# ================= [核心] 数据库连接获取 =================
# ================= [核心修复] 数据库连接获取 (带自动续命功能) =================
def get_db():
    # 1. 上帝模式检查
    if session.get('is_impersonator') and admin_supabase:
        return admin_supabase

    # 2. 普通用户模式
    token = session.get('access_token')
    refresh_token = session.get('refresh_token')

    if token and refresh_token:
        try:
            # 创建临时客户端
            auth_client = create_client(url, key)

            # 尝试建立会话
            # 注意：set_session 可能会校验 token，如果过期会抛出异常
            auth_client.auth.set_session(token, refresh_token)

            # 这里做一个极小的查询测试 Token 是否真的有效
            # (Supabase py SDK 有时候 set_session 不报错但请求时才报错)
            # 我们不真查数据，只为了触发验证
            return auth_client

        except Exception as e:
            # === 触发自动续命逻辑 ===
            print(f"⚠️ Token 可能过期，尝试自动刷新... ({e})")

            try:
                # 使用 refresh_token 换取新的 access_token
                # 注意：这里要用全局 supabase 客户端来执行刷新
                res = supabase.auth.refresh_session(refresh_token)

                if res.session:
                    # 1. 救活了！更新 Session 里的 Token
                    session['access_token'] = res.session.access_token
                    session['refresh_token'] = res.session.refresh_token

                    # 2. 重新创建带新 Token 的客户端返回
                    new_client = create_client(url, key)
                    new_client.auth.set_session(res.session.access_token, res.session.refresh_token)
                    print("✅ Token 自动刷新成功！")
                    return new_client
            except Exception as refresh_error:
                print(f"❌ 自动刷新失败，彻底登出: {refresh_error}")

    # 3. 彻底没救了，清空 Session，让用户重登
    session.clear()


# ================= 装饰器 =================

def login_required(f):
    """强制登录装饰器"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))

        # 1. 上帝模式：直接放行
        if session.get('is_impersonator'):
            return f(*args, **kwargs)

        # 2. [核心优化] 优先检查 Session 缓存
        # 如果登录时已经确认是 admin，直接放行，不查数据库！
        if session.get('role') == 'admin':
            return f(*args, **kwargs)

        # 3. 兜底：如果 Session 里没存 (比如旧登录状态)，再去查一次数据库
        try:
            client = get_db() or supabase
            res = client.table('profiles').select('role').eq('id', session['user']).single().execute()

            if res.data and res.data['role'] == 'admin':
                # 查到了，顺手补进 Session，下次就快了
                session['role'] = 'admin'
                return f(*args, **kwargs)
            else:
                # 确实不是管理员
                flash("🚫 权限拒绝：你没有管理员权限！", "danger")
                return redirect(url_for('home'))

        except Exception as e:
            # 查库报错了 (网络抖动等)
            print(f"Admin Check Error: {e}")
            flash("⚠️ 权限验证超时，请重试或重新登录", "warning")
            return redirect(url_for('home'))

    return decorated_function


@app.context_processor
def inject_version():
    return dict(app_version=CURRENT_APP_VERSION)


# ================= 认证路由 =================
@app.route('/register', methods=['GET', 'POST'])
@csrf.exempt
def register():
    if request.method == 'POST':
        account = request.form.get('account')
        password = request.form.get('password')
        name = request.form.get('display_name')
        secret_code = request.form.get('secret_code')

        # [修改] 使用 Service Key 检查暗号有效性
        # 因为注册用户此时未登录，无法通过 RLS，必须用 admin_supabase
        if not admin_supabase:
            flash("系统配置错误：缺少 Service Key", "danger")
            return render_template('register.html')

        try:
            # 1. 查询暗号是否存在且有剩余次数
            code_res = admin_supabase.table('registration_codes') \
                .select('*').eq('code', secret_code).single().execute()

            if not code_res.data:
                flash("注册暗号无效！", "danger")
                return render_template('register.html')

            code_data = code_res.data
            if code_data['current_uses'] >= code_data['max_uses']:
                flash("该暗号已被用完，请联系管理员获取新暗号。", "warning")
                return render_template('register.html')

            # 2. 执行注册
            res = supabase.auth.sign_up({
                "email": resolve_account(account),
                "password": password,
                "options": {"data": {"display_name": name}}
            })

            if res.user:
                # 3. [关键] 注册成功后，暗号使用次数 +1
                new_count = code_data['current_uses'] + 1
                admin_supabase.table('registration_codes') \
                    .update({'current_uses': new_count}) \
                    .eq('id', code_data['id']).execute()

                flash("注册成功！请直接登录。", "success")
                return redirect(url_for('login'))

        except Exception as e:
            flash(f"注册失败: {str(e)}", "danger")

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
@csrf.exempt
def login():
    if request.method == 'POST':
        account = request.form.get('account')
        password = request.form.get('password')
        try:
            # 1. 登录请求 (这一步最耗时)
            final_email = resolve_account(account)
            res = supabase.auth.sign_in_with_password({"email": final_email, "password": password})

            # 2. 设置 Session (极速版)
            session.permanent = True
            session.clear()
            session['user'] = res.user.id
            session['email'] = res.user.email
            session['access_token'] = res.session.access_token
            session['refresh_token'] = res.session.refresh_token

            # [核心修改] 3. 查一次 Profile，把 昵称 和 身份(role) 都存进 Session
            # 这样以后就不用每次都查库了，极快且稳
            try:
                # 使用全局 supabase 查，因为刚登录 token 可能还没热乎
                p = supabase.table('profiles').select("display_name, role").eq('id', res.user.id).single().execute()
                if p.data:
                    session['display_name'] = p.data.get('display_name', "家人")
                    session['role'] = p.data.get('role', 'user')  # <--- 关键！存入 role
            except Exception as e:
                print(f"Profile Load Error: {e}")
                session['display_name'] = "家人"
                session['role'] = 'user'
            # [优化] 优先从 Auth Metadata 获取昵称，不查数据库，极大提升速度
            meta_name = res.user.user_metadata.get('display_name')
            session['display_name'] = meta_name if meta_name else "家人"

            return redirect(url_for('home'))

        except Exception as e:
            print(f"Login Error: {e}")  # 方便在 Vercel 后台看日志
            flash("登录超时或失败，请检查账号和密码", "danger")

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    supabase.auth.sign_out()
    return redirect(url_for('login'))


# ================= 核心业务路由 (Home/Action) =================

# --- 修改后的 home 函数 ---
@app.route('/')
@login_required
def home():
    """
    主页路由 (完美整合版)
    修复了 user_map 报错，补全了足迹功能，恢复了高精度时间逻辑
    """
    current_user_id = session.get('user')
    current_tab = request.args.get('tab', 'pets')
    # 仅用于显示日期的字符串，数据库查询使用后面更严谨的 UTC 逻辑
    today_str = get_beijing_time().strftime('%Y-%m-%d')
    db = get_db()

    if db is None: return redirect(url_for('login'))

    # ================= 1. 获取"我自己"的档案 & 家庭列表 =================
    my_profile = {}
    my_family_ids = []
    my_families = []

    try:
        res = db.table('profiles').select("*").eq('id', current_user_id).maybe_single().execute()
        if res.data:
            my_profile = res.data
            if my_profile.get('avatar_url'):
                my_profile[
                    'full_avatar_url'] = f"{url}/storage/v1/object/public/family_photos/{my_profile['avatar_url']}"

            members_res = db.table('family_members').select('family_id').eq('user_id', current_user_id).execute()
            if members_res.data:
                my_family_ids = [m['family_id'] for m in members_res.data]
                if my_family_ids:
                    fams_res = db.table('families').select('*').in_('id', my_family_ids).execute()
                    my_families = fams_res.data or []
    except Exception as e:
        print(f"Profile Error: {e}")

    if my_profile.get('display_name'): session['display_name'] = my_profile['display_name']
    user_name = session.get('display_name', '家人')

    # ================= 2. 获取可见成员映射 (顺序修复核心) =================
    # 必须在遍历家庭处理优惠券之前生成，否则会报错
    user_map = {}
    family_members_dict = {}
    try:
        if my_family_ids:
            co_members = db.table('family_members').select('family_id, user_id').in_('family_id',
                                                                                     my_family_ids).execute()
            visible_user_ids = list(set([m['user_id'] for m in co_members.data]))

            for m in co_members.data:
                fid = m['family_id']
                uid = m['user_id']
                if fid not in family_members_dict: family_members_dict[fid] = []
                family_members_dict[fid].append(uid)

            if visible_user_ids:
                profiles_res = db.table('profiles').select("id, display_name, avatar_url, status").in_('id',
                                                                                                       visible_user_ids).execute()
                for p in profiles_res.data:
                    avatar_link = None
                    if p.get('avatar_url'):
                        avatar_link = f"{url}/storage/v1/object/public/family_photos/{p['avatar_url']}"
                    user_map[p['id']] = {
                        'name': p['display_name'],
                        'avatar': avatar_link,
                        'status': p.get('status', 'online')
                    }
        else:
            p = my_profile
            user_map[p.get('id')] = {'name': p.get('display_name'), 'avatar': p.get('full_avatar_url'),
                                     'status': 'online'}
    except:
        pass

    # ================= 3. 遍历家庭，填充各类工具箱数据 =================
    bj_now_date = datetime.now(timezone(timedelta(hours=8))).date()
    utc_now = datetime.now(timezone.utc)

    for f in my_families:
        # --- A. 倒计时 & 纪念日 ---
        f['top_event'] = None
        f['all_events'] = []
        candidate_events = []

        # 1. 归家倒计时
        if f.get('reunion_date'):
            try:
                target = datetime.strptime(f['reunion_date'], '%Y-%m-%d').date()
                days = (target - bj_now_date).days
                if days >= 0:
                    candidate_events.append({'title': f.get('reunion_name') or '团圆',
                                             'data': {'days': days, 'total': 0, 'date_str': f['reunion_date'],
                                                      'is_repeat': False}, 'type': 'reunion'})
            except:
                pass

        # 2. 家庭大事记
        try:
            db_events = db.table('family_events').select('*').eq('family_id', f['id']).execute().data or []
            for e in db_events:
                calc = calculate_event_details(e)
                if calc and (calc['days'] >= 0 or calc['total'] > 0):
                    candidate_events.append({'id': e['id'], 'title': e['title'], 'data': calc, 'type': 'event',
                                             'is_lunar': e['event_type'] == 'lunar'})
        except:
            pass

        if candidate_events:
            candidate_events.sort(key=lambda x: (1 if x['data']['days'] < 0 else 0, abs(x['data']['days'])))
            f['top_event'] = candidate_events[0]
            f['all_events'] = candidate_events

        # --- B. 天气缓存 ---
        f['weather_home'] = f.get('weather_data_home')
        f['weather_away'] = f.get('weather_data_away')
        need_update = False
        if not f.get('last_weather_update'):
            need_update = True
        else:
            try:
                last_t = datetime.fromisoformat(f.get('last_weather_update').replace('Z', '+00:00'))
                if (utc_now - last_t) > timedelta(minutes=30): need_update = True
            except:
                need_update = True

        if need_update:
            nh = get_weather_full(f.get('location_home_id'), f.get('location_home_lat'), f.get('location_home_lon'))
            na = get_weather_full(f.get('location_away_id'), f.get('location_away_lat'), f.get('location_away_lon'))
            if nh: f['weather_home'] = nh
            if na: f['weather_away'] = na
            if nh or na:
                try:
                    payload = {'last_weather_update': utc_now.isoformat()}
                    if nh: payload['weather_data_home'] = nh
                    if na: payload['weather_data_away'] = na
                    db.table('families').update(payload).eq('id', f['id']).execute()
                except:
                    pass

        # --- [关键补回] C. 足迹列表 (Footprints) ---
        f['footprints'] = []
        try:
            fp_res = db.table('family_footprints').select('*').eq('family_id', f['id']).execute()
            f['footprints'] = fp_res.data or []
        except:
            pass

        # --- D. 许愿菜单 ---
        f['wishes'] = []
        try:
            w_res = db.table('family_wishes').select('*').eq('family_id', f['id']).order('created_at',
                                                                                         desc=True).execute()
            raw_w = w_res.data or []
            status_order = {'wanted': 0, 'bought': 1, 'eaten': 2}
            f['wishes'] = sorted(raw_w, key=lambda x: status_order.get(x['status'], 0))
        except:
            pass

        # --- E. 家庭提醒 (留言板) ---
        f['reminders'] = []
        try:
            yesterday = (utc_now - timedelta(hours=24)).isoformat()

            # 1. 先查出最近的提醒 (这里 RLS 可能会返回"我发给别人的"，所以需要后续过滤)
            r_res = db.table('family_reminders') \
                .select('*') \
                .eq('family_id', f['id']) \
                .gte('created_at', yesterday) \
                .order('created_at', desc=True) \
                .limit(10) \
                .execute()  # limit 稍微拿多一点，防止过滤后不够3条

            raw_rems = r_res.data or []
            valid_rems = []

            # 2. [核心修复] Python 层过滤：只看 "发给我的" 或 "公开的"
            for r in raw_rems:
                target = r.get('target_user_id')

                # 过滤规则：
                # 如果有目标人，且目标人不是我 -> 跳过 (这是我发给别人的，或者是别人发给别人的)
                if target and target != current_user_id:
                    continue

                # 时间格式化
                try:
                    dt_utc = datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
                    r['time_display'] = dt_utc.astimezone(timezone(timedelta(hours=8))).strftime('%H:%M')
                except:
                    r['time_display'] = ""

                valid_rems.append(r)

                # 只取前3条显示，多了没必要
                if len(valid_rems) >= 3: break

            f['reminders'] = valid_rems
        except Exception as e:
            print(f"Reminders Error: {e}")

        # --- F. 收纳 / 采购 / 兑换券 / Wi-Fi / 备忘录 ---
        f['inventory'] = []
        f['shopping_list'] = []
        f['coupons_received'] = []
        f['coupons_sent'] = []
        f['wifis'] = []
        f['memos'] = []

        try:
            # 收纳
            inv = db.table('family_inventory').select('*').eq('family_id', f['id']).order('created_at',
                                                                                          desc=True).execute()
            f['inventory'] = inv.data or []
            for i in f['inventory']:
                if i.get('image_path'): i['url'] = f"{url}/storage/v1/object/public/family_photos/{i['image_path']}"

            # 采购
            shop = db.table('family_shopping_list').select('*').eq('family_id', f['id']).order('created_at',
                                                                                               desc=True).execute()
            shop_d = shop.data or []
            f['shopping_list'] = sorted(shop_d, key=lambda x: x.get('is_bought', False))

            # Wi-Fi
            wf = db.table('family_wifis').select('*').eq('family_id', f['id']).execute()
            f['wifis'] = wf.data or []

            # 备忘录 (解密)
            mm = db.table('family_memos').select('*').eq('family_id', f['id']).execute()
            memos = mm.data or []
            for m in memos:
                m['content'] = decrypt_data(m['content'])
            f['memos'] = memos

            # 兑换券 (此时 user_map 已存在，安全)
            coupons = db.table('family_coupons').select('*').eq('family_id', f['id']).order('created_at',
                                                                                            desc=True).execute()
            for c in (coupons.data or []):
                c['creator_name'] = user_map.get(c['creator_id'], {}).get('name', '神秘人')
                c['target_name'] = user_map.get(c['target_user_id'], {}).get('name', '某人')
                if c['target_user_id'] == current_user_id: f['coupons_received'].append(c)
                if c['creator_id'] == current_user_id: f['coupons_sent'].append(c)
        except:
            pass

    # ================= 4. 获取宠物、日志、动态 =================
    pets = []
    logs = []
    moments_data = []
    pet_owners_map = {}

    try:
        if my_family_ids:
            # 宠物
            pets = db.table('pets').select("*").in_('family_id', my_family_ids).order('id').execute().data or []

            # 宠物主人
            all_pet_ids = [p['id'] for p in pets]
            if all_pet_ids:
                all_owners = db.table('pet_owners').select('pet_id, user_id').in_('pet_id', all_pet_ids).execute()
                for o in (all_owners.data or []):
                    pid = o['pet_id']
                    if pid not in pet_owners_map: pet_owners_map[pid] = []
                    pet_owners_map[pid].append(o['user_id'])

            # 日志 (恢复原版高精度时间逻辑，解决时区BUG)
            if all_pet_ids:
                # 1. 获取当前北京时间
                now_bj = datetime.now(timezone(timedelta(hours=8)))
                # 2. 拿到今天 00:00:00 的时间点
                today_start_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
                # 3. 转回 UTC 时间 (这才是数据库能看懂的"今天开始")
                filter_time_utc = today_start_bj.astimezone(timezone.utc).isoformat()

                logs = db.table('logs').select("*") \
                           .in_('pet_id', all_pet_ids) \
                           .gte('created_at', filter_time_utc) \
                           .order('created_at', desc=True) \
                           .execute().data or []

            # 动态
            moms_res = db.table('moments').select("*").in_('user_id', list(user_map.keys())).order('created_at',
                                                                                                   desc=True).limit(
                20).execute()
            moments_data = moms_res.data or []
    except Exception as e:
        print(f"Data Fetch Error: {e}")

    # ================= 5. 数据二次组装 (前端渲染用) =================

    # A. 宠物
    for pet in pets:
        pet['today_feed'] = False;
        pet['today_walk'] = False
        pet['feed_info'] = "";
        pet['walk_info'] = ""
        pet['latest_photo'] = None;
        pet['photo_uploader'] = "";
        pet['photo_count'] = 0

        pet['owner_ids'] = pet_owners_map.get(pet['id'], [])
        pet['is_owner'] = (current_user_id in pet['owner_ids']) or session.get('is_impersonator')

        fam_obj = next((f for f in my_families if f['id'] == pet['family_id']), None)
        pet['family_name'] = fam_obj['name'] if fam_obj else ""

        for log in logs:
            if log['pet_id'] == pet['id']:
                who = user_map.get(log['user_id'], {}).get('name', '家人')
                time_s = format_time_friendly(log['created_at'])
                if log['action'] == 'feed':
                    pet['today_feed'] = True
                    if not pet['feed_info']: pet['feed_info'] = f"{who} ({time_s})"
                elif log['action'] == 'walk':
                    pet['today_walk'] = True
                    if not pet['walk_info']: pet['walk_info'] = f"{who} ({time_s})"
                elif log['action'] == 'photo':
                    pet['photo_count'] += 1
                    if not pet['latest_photo'] and log.get('image_path'):
                        pet['latest_photo'] = f"{url}/storage/v1/object/public/family_photos/{log['image_path']}"
                        pet['photo_uploader'] = who

    # B. 动态 (加点赞人)
    moments = []
    for m in moments_data:
        # 基本信息
        u_info = user_map.get(m['user_id'], {})
        m['user_name'] = u_info.get('name', '家人')
        m['user_avatar'] = u_info.get('avatar')
        m['time_str'] = format_time_friendly(m['created_at'])
        if m.get('image_path'):
            m['image_url'] = f"{url}/storage/v1/object/public/family_photos/{m['image_path']}"

        # 点赞信息
        try:
            likes_res = db.table('moment_likes').select('user_id').eq('moment_id', m['id']).execute()
            likes_data = likes_res.data or []
            m['likers'] = []
            m['is_liked'] = False
            for l in likes_data:
                uid = l['user_id']
                if uid == current_user_id: m['is_liked'] = True
                if uid in user_map: m['likers'].append(user_map[uid])
            m['like_count'] = len(m['likers'])
        except:
            pass

        moments.append(m)

    # 6. 获取更新日志
    latest_update = None
    try:
        up_res = db.table('app_updates').select('*').eq('is_pushed', True).order('created_at', desc=True).limit(
            1).execute()
        if up_res.data:
            latest_update = up_res.data[0]
            latest_update['content'] = latest_update['content'].replace('\n', '<br>')
    except:
        pass

    if session.get('is_impersonator'):
        flash(f"👁️ 上帝模式：{user_name}", "info")

    return render_template('home.html',
                           pets=pets, moments=moments, user_name=user_name,
                           current_user_id=current_user_id,
                           current_role=my_profile.get('role', 'user'),
                           my_profile=my_profile, my_families=my_families,
                           user_map=user_map, family_members_dict=family_members_dict,
                           current_tab=current_tab, today=today_str,
                           latest_update=latest_update)

# ================= 宠物详情页模块 =================
@app.route('/pet/<int:pet_id>')
@login_required
def pet_detail(pet_id):
    """宠物详情页"""
    db = get_db()

    # 1. 获取宠物基础信息
    pet = {}
    try:
        res = db.table('pets').select('*').eq('id', pet_id).single().execute()
        if res.data:
            pet = res.data
            # 计算年龄
            pet['age_display'] = calculate_age(pet.get('birthday'))

            # 处理图片链接 (头像和封面)
            # 如果没有专门设封面，就用最新的一张照片当封面，还没有就用默认图
            cover_path = pet.get('cover_image')

            # 2. 获取这只宠物的照片墙 (Logs)
            logs_res = db.table('logs').select('*') \
                .eq('pet_id', pet_id) \
                .eq('action', 'photo') \
                .order('created_at', desc=True) \
                .execute()

            photos = logs_res.data or []

            # 补全图片URL + [新增] 转换显示时间
            for p in photos:
                if p.get('image_path'):
                    p['url'] = f"{url}/storage/v1/object/public/family_photos/{p['image_path']}"
                # [新增] UTC -> 北京时间
                try:
                    # 解析数据库时间
                    dt_utc = datetime.fromisoformat(p['created_at'].replace('Z', '+00:00'))
                    # 转北京时间
                    dt_bj = dt_utc.astimezone(timezone(timedelta(hours=8)))
                    # 存一个新的字段用于显示 (格式: 2025-12-16 10:30)
                    p['display_time'] = dt_bj.strftime('%Y-%m-%d %H:%M')
                    # 也可以只存日期用于拍立得底部
                    p['display_date'] = dt_bj.strftime('%Y-%m-%d')
                except:
                    p['display_time'] = "时间未知"
                    p['display_date'] = "Unknown"

            # 智能决定封面：有设定用设定，没设定用最新照片
            if cover_path:
                pet['cover_url'] = f"{url}/storage/v1/object/public/family_photos/{cover_path}"
            elif photos:
                pet['cover_url'] = photos[0]['url']
            else:
                # 默认封面 (可以是网图或者本地图)
                pet['cover_url'] = "/static/default_cover.png"  # 暂时用个占位，或者前端CSS处理

            pet['photos'] = photos

            # 3. 检查我是不是主人 (用于显示编辑按钮)
            is_owner = False
            owner_res = db.table('pet_owners').select('user_id').eq('pet_id', pet_id).execute()
            if owner_res.data:
                owner_ids = [o['user_id'] for o in owner_res.data]
                if session['user'] in owner_ids or session.get('is_impersonator'):
                    is_owner = True
            pet['is_owner'] = is_owner

    except Exception as e:
        print(f"Pet Detail Error: {e}")
        return redirect(url_for('home'))
    my_profile = {}
    try:
        prof_res = db.table('profiles').select('*').eq('id', session['user']).maybe_single().execute()
        if prof_res.data:
            my_profile = prof_res.data
    except:
        pass

    return render_template('pet_detail.html',
                            pet=pet,
                            current_user_id=session['user'],
                           my_profile=my_profile,  # <--- 关键修复
                           app_version=CURRENT_APP_VERSION
                           )  


@app.route('/update_pet_detail', methods=['POST'])
@login_required
def update_pet_detail():
    """更新宠物详细档案"""
    db = get_db()
    pet_id = request.form.get('pet_id')

    data = {
        'birthday': request.form.get('birthday') or None,
        'weight': request.form.get('weight') or None,
        'vaccine_date': request.form.get('vaccine_date') or None,
        'deworm_date': request.form.get('deworm_date') or None,
        'gender': request.form.get('gender') or 'unknown'
    }

    try:
        db.table('pets').update(data).eq('id', pet_id).execute()
        flash("档案更新成功！", "success")
    except Exception as e:
        flash(f"更新失败: {e}", "danger")

    return redirect(url_for('pet_detail', pet_id=pet_id))


@app.route('/action', methods=['POST'])
@login_required
def log_action():
    """喂食/遛狗打卡"""
    try:
        db = get_db()
        pet_id = request.form.get('pet_id')
        action = request.form.get('action')

        # 打印调试信息 (Vercel Logs 里能看到)
        print(f"Action: {action}, Pet: {pet_id}, User: {session['user']}")

        if not pet_id or not action:
            flash("参数缺失，请刷新页面重试", "warning")
            return redirect(url_for('home', tab='pets'))

        # 执行插入
        db.table('logs').insert({
            "pet_id": pet_id,
            "user_id": session['user'],
            "action": action
        }).execute()

        # 成功提示 (可选，为了不打扰用户通常不提示成功，只提示失败)
        # flash("打卡成功", "success")

    except Exception as e:
        # 把错误显示在页面上，如果是 42501 就是权限问题
        print(f"Log Action Error: {e}")
        flash(f"打卡失败: {e}", "danger")

    return redirect(url_for('home', tab='pets'))


@app.route('/upload_pet', methods=['POST'])
@login_required
def upload_pet_photo():
    """上传宠物照片"""
    try:
        db = get_db()
        f = request.files.get('photo')
        pet_id = request.form.get('pet_id')

        if not f or not f.filename:
            flash("请选择照片", "warning")
            return redirect(url_for('home', tab='pets'))

        # 生成安全的文件名
        filename = secure_filename(f.filename)
        # 如果中文文件名导致为空，使用随机名
        if not filename:
            filename = "image.jpg"

        file_path = f"pet_{int(datetime.now().timestamp())}_{filename}"

        # 1. 上传文件
        # 读取文件内容
        file_content = f.read()
        db.storage.from_("family_photos").upload(
            file_path,
            file_content,
            {"content-type": f.content_type}
        )

        # 2. 写入数据库
        db.table('logs').insert({
            "pet_id": pet_id,
            "user_id": session['user'],
            "action": "photo",
            "image_path": file_path
        }).execute()

        flash("照片上传成功", "success")

    except Exception as e:
        print(f"Upload Error: {e}")
        flash(f"上传失败: {e}", "danger")

    return redirect(url_for('home', tab='pets'))


@app.route('/post_moment', methods=['POST'])
@login_required
def post_moment():
    """发布动态 (支持分组可见)"""
    try:
        db = get_db()
        content = request.form.get('content')
        f = request.files.get('photo')
        # 获取可见性设置：'public' 或具体的 family_id
        visibility = request.form.get('visibility')

        # 构造插入数据
        data = {
            "user_id": session['user'],
            "content": content
        }

        # 处理可见性逻辑
        if visibility and visibility != 'public':
            data['target_family_id'] = visibility
        else:
            data['target_family_id'] = None  # 公开

        # 处理图片上传
        if f and f.filename:
            filename = secure_filename(f.filename)
            file_path = f"moment_{int(datetime.now().timestamp())}_{filename}"

            db.storage.from_("family_photos").upload(
                file_path,
                f.read(),
                {"content-type": f.content_type}
            )
            data['image_path'] = file_path

        # 写入数据库
        if content or f:
            db.table('moments').insert(data).execute()

    except Exception as e:
        flash(f"发布失败: {e}", "danger")

    return redirect(url_for('home', tab='life'))


@app.route('/delete_log/<int:log_id>', methods=['POST'])
@login_required
def delete_log(log_id):
    try:
        db = get_db()
        res = db.table('logs').select("image_path, user_id").eq('id', log_id).execute()
        if res.data:
            rec = res.data[0]
            if rec['user_id'] == session['user']:
                if rec.get('image_path'): db.storage.from_("family_photos").remove(rec['image_path'])
                db.table('logs').delete().eq('id', log_id).execute()
    except:
        pass
    return redirect(url_for('home', tab='pets'))


@app.route('/delete_moment/<int:mid>', methods=['POST'])
@login_required
def delete_moment(mid):
    try:
        db = get_db()
        res = db.table('moments').select("image_path, user_id").eq('id', mid).execute()
        if res.data:
            rec = res.data[0]
            if rec['user_id'] == session['user']:
                if rec.get('image_path'): db.storage.from_("family_photos").remove(rec['image_path'])
                db.table('moments').delete().eq('id', mid).execute()
    except:
        pass
    return redirect(url_for('home', tab='life'))


# ================= 家庭管理路由 (新增) =================
@app.route('/set_reunion', methods=['POST'])
@login_required
def set_reunion():
    """设置归家倒计时"""
    db = get_db()
    family_id = request.form.get('family_id')
    reunion_name = request.form.get('reunion_name')
    reunion_date = request.form.get('reunion_date')

    # 如果没填日期，视为“取消/清除”倒计时
    if not reunion_date:
        update_data = {'reunion_date': None, 'reunion_name': None}
        msg = "已取消倒计时"
    else:
        update_data = {'reunion_date': reunion_date, 'reunion_name': reunion_name or "团圆"}
        msg = "倒计时设置成功！"

    try:
        # RLS 会保证只有成员能改
        db.table('families').update(update_data).eq('id', family_id).execute()
        flash(msg, "success")
    except Exception as e:
        flash(f"设置失败: {e}", "danger")

    return redirect(url_for('home'))


@app.route('/set_weather_city', methods=['POST'])
@login_required
def set_weather_city():
    db = get_db()
    family_id = request.form.get('family_id')
    type_ = request.form.get('type')
    city_name = request.form.get('city_name')

    if not city_name:
        # 清除逻辑
        update_data = {
            f'location_{type_}_id': None,
            f'location_{type_}_name': None,
            f'location_{type_}_lat': None,  # 清除经纬度
            f'location_{type_}_lon': None
        }
        flash(f"已清除该城市设置", "info")
    else:
        # [关键修改] 接收 4 个返回值
        cid, cname, lat, lon = search_city_qweather(city_name)

        if not cid:
            flash(f"找不到城市 '{city_name}'", "warning")
            return redirect(url_for('home'))

        # 保存 ID (给天气/指数用) 和 Lat/Lon (给空气用)
        update_data = {
            f'location_{type_}_id': cid,
            f'location_{type_}_name': cname,
            f'location_{type_}_lat': lat,
            f'location_{type_}_lon': lon
        }
        msg = f"已设置{type_}城市为：{cname}"

    try:
        db.table('families').update(update_data).eq('id', family_id).execute()
        if city_name: flash(msg, "success")
    except Exception as e:
        flash(f"设置失败: {e}", "danger")

    return redirect(url_for('home'))


@app.route('/send_family_reminder', methods=['POST'])
@login_required
def send_family_reminder():
    db = get_db()
    family_id = request.form.get('family_id')
    content = request.form.get('content')
    if not content: return redirect(url_for('home'))

    try:
        current_user_id = session['user']

        # [修改] 频率限制逻辑：只查“我自己”在这个家庭发的最新一条
        last_rem = db.table('family_reminders') \
            .select('created_at, content') \
            .eq('family_id', family_id) \
            .eq('created_by', current_user_id) \
            .order('created_at', desc=True) \
            .limit(5) \
            .execute()

        if last_rem.data:
            for rem in last_rem.data:
                # [核心修复] 如果这条记录是"拍一拍"或者是"兑换券"通知，跳过，不计入冷却
                if "拍了拍" in rem['content'] or "给你发了" in rem['content'] or "作废" in rem['content']:
                    continue
            # [核心修复] 手动解析时间，防止毫秒位数不对导致报错
                try:
                    raw_time = last_rem.data[0]['created_at']
                    # 1. 简单粗暴：只截取前19位 (YYYY-MM-DDTHH:MM:SS)
                    # 这样就丢掉了 ".63411+00:00" 这种可能导致报错的尾巴
                    clean_time = raw_time[:19]
                    # 2. 解析为时间对象
                    dt_obj = datetime.strptime(clean_time, '%Y-%m-%dT%H:%M:%S')

                    # 3. 补上 UTC 时区 (因为数据库存的是 UTC)
                    dt_utc = dt_obj.replace(tzinfo=timezone.utc)

                    # 4. 转为北京时间
                    last_date = dt_utc.astimezone(timezone(timedelta(hours=8))).date()

                    # 5. 获取今天日期
                    today_date = datetime.now(timezone(timedelta(hours=8))).date()

                # 6. 比对
                    if last_date == today_date:
                        flash("你今天在这个家已经发过提醒啦 (每人每天限1条)", "info")
                        return redirect(url_for('home'))
                    break

                except Exception as e:
                    print(f"Time Parse Error: {e}")
                    pass

        # ... (插入逻辑) ...
        sender_name = session.get('display_name', '家人')

        # [修改] 插入时带上 created_by
        db.table('family_reminders').insert({
            'family_id': family_id,
            'content': content,
            'sender_name': sender_name,
            'created_by': current_user_id  # <--- 关键：记录是谁发的
        }).execute()

        # 微信推送
        send_wechat_push(
            family_id=family_id,
            summary=f"🔔 {sender_name} 发了一条提醒",
            content=f"来自 {sender_name} 的叮嘱：\n\n{content}\n\n快去App看看吧！"
        )
        flash("提醒已发送", "success")
    except Exception as e:
        flash(f"发送失败: {e}", "danger")

    return redirect(url_for('home'))


@app.route('/create_family', methods=['POST'])
@login_required
def create_family():
    # ⚠️ 关键修改：优先使用 admin_supabase (上帝权限)
    # 这样可以绕过 "必须先是成员才能看到家庭ID" 的 RLS 死锁问题
    # 如果只用 get_db()，在插入 members 时可能会因为你还不是 member 而被拒绝
    if admin_supabase:
        client = admin_supabase
    else:
        client = get_db()
        print("⚠️ 警告: 缺少 Service Key，创建家庭可能会失败")

    family_name = request.form.get('family_name')

    if not family_name:
        flash("家庭名称不能为空", "warning")
        return redirect(url_for('home', tab='mine'))

    try:
        code = generate_invite_code()

        # 1. 使用上帝权限插入家庭，获取 ID
        # execute() 后直接返回数据列表
        res = client.table('families').insert({
            "name": family_name,
            "invite_code": code
        }).execute()

        if res.data and len(res.data) > 0:
            new_fam_id = res.data[0]['id']

            # 2. [核心修复] 依然使用上帝权限，把自己绑定进这个家庭
            # 这一步至关重要，不加这一步，新家庭在首页就是空的
            client.table('family_members').insert({
                'family_id': new_fam_id,
                'user_id': session['user']
            }).execute()

            flash(f"🎉 家庭 [{family_name}] 创建成功！邀请码是 {code}", "success")
        else:
            flash("创建失败，数据库未返回数据", "danger")

    except Exception as e:
        flash(f"创建失败: {e}", "danger")

    return redirect(url_for('home', tab='mine'))


@app.route('/join_family', methods=['POST'])
@login_required
def join_family():
    # 加入家庭需要查询邀请码，必须用 admin 权限查 (因为你还没加入，看不到别的家庭)
    if not admin_supabase:
        flash("缺少 Service Key，无法查询邀请码", "danger")
        return redirect(url_for('home', tab='mine'))

    code = request.form.get('invite_code')
    if not code: return redirect(url_for('home', tab='mine'))

    try:
        # 1. 查家庭 ID
        fam = admin_supabase.table('families').select('id, name').eq('invite_code', code.upper()).single().execute()

        if fam.data:
            target_id = fam.data['id']

            # 2. [修改] 插入中间表
            # 这里可以用 get_db()，因为 RLS 策略通常允许用户 insert 自己的 member 记录
            try:
                get_db().table('family_members').insert({
                    'family_id': target_id,
                    'user_id': session['user']
                }).execute()

                flash(f"成功加入 [{fam.data['name']}]！", "success")
            except Exception as e:
                # 捕获重复加入的错误
                if "duplicate" in str(e) or "Unique" in str(e) or "23505" in str(e):
                    flash("你已经在该家庭里了，无需重复加入", "info")
                else:
                    print(f"Join Error: {e}")
                    flash(f"加入失败: {str(e)}", "danger")
        else:
            flash("邀请码无效，请检查输入", "warning")

    except Exception as e:
        flash(f"系统错误: {e}", "danger")

    return redirect(url_for('home', tab='mine'))


@app.route('/leave_family', methods=['POST'])
@login_required
def leave_family():
    db = get_db()
    family_id = request.form.get('family_id')  # 前端必须传 family_id

    try:
        # [修改] 删除中间表记录
        db.table('family_members').delete().eq('family_id', family_id).eq('user_id', session['user']).execute()
        flash("已退出该家庭", "info")
    except Exception as e:
        flash(f"退出失败: {e}", "danger")
    return redirect(url_for('home', tab='mine'))


# ================= 个人信息管理路由 =================

@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    db = get_db()
    display_name = request.form.get('display_name')
    f = request.files.get('avatar')
    is_elder = request.form.get('is_elder_mode') == 'on'
    wx_uid = request.form.get('wx_uid')
    is_dark = request.form.get('is_dark_mode') == 'on'

    update_data = {'is_elder_mode': is_elder,'is_dark_mode': is_dark}
    if display_name: update_data['display_name'] = display_name
    if wx_uid is not None: update_data['wx_uid'] = wx_uid.strip()

    if f and f.filename:
        try:
            # [新增] 先查旧头像，准备删除
            old_prof = db.table('profiles').select('avatar_url').eq('id', session['user']).single().execute()
            if old_prof.data and old_prof.data.get('avatar_url'):
                try:
                    db.storage.from_("family_photos").remove(old_prof.data['avatar_url'])
                except:
                    pass  # 删失败也不影响新头像

            # 上传新头像
            filename = secure_filename(f.filename)
            file_path = f"avatar_{session['user']}_{int(datetime.now().timestamp())}_{filename}"
            db.storage.from_("family_photos").upload(file_path, f.read(), {"content-type": f.content_type})
            update_data['avatar_url'] = file_path
        except Exception as e:
            flash(f"头像上传失败: {e}", "danger")

    try:
        db.table('profiles').update(update_data).eq('id', session['user']).execute()
        flash("设置已更新", "success")
    except Exception as e:
        flash(f"更新失败: {e}", "danger")

    return redirect(url_for('home', tab='mine'))


@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    """修改密码"""
    new_password = request.form.get('new_password')
    db = get_db()

    if new_password and len(new_password) >= 6:
        try:
            # 如果是上帝模式，这里不能用 db.auth.update_user (因为那是改当前 token 用户的)
            # 必须用 admin_supabase.auth.admin.update_user_by_id
            if session.get('is_impersonator') and admin_supabase:
                admin_supabase.auth.admin.update_user_by_id(session['user'], {"password": new_password})
                flash("【上帝模式】已强制修改该用户密码", "warning")
            else:
                # 普通用户修改自己
                db.auth.update_user({"password": new_password})
                flash("密码修改成功，下次请用新密码登录", "success")
        except Exception as e:
            flash(f"修改失败: {e}", "danger")
    else:
        flash("密码太短啦，至少6位", "warning")

    return redirect(url_for('home', tab='mine'))


# ================= 后台管理系统路由 (Admin) =================

@app.route('/admin')
@admin_required
def admin_dashboard():
    """后台首页：集成了多家庭、宠物主人、更新日志、文件分析的完整版"""
    # 管理员始终拥有最高权限 (Service Key)
    client = admin_supabase if admin_supabase else supabase

    # 1. 批量获取所有数据 (加了 try-except 防止某张表没建导致崩盘)
    try:
        users = client.table('profiles').select("*").order('created_at', desc=True).execute().data
        pets = client.table('pets').select("*").order('id').execute().data
        families = client.table('families').select("*").order('id').execute().data
        # 中间表数据
        members = client.table('family_members').select('*').execute().data or []
        pet_owners_data = client.table('pet_owners').select('*').execute().data or []
        # 更新日志数据
        updates_list = client.table('app_updates').select('*').order('created_at', desc=True).execute().data or []
        reg_codes = client.table('registration_codes').select('*').order('created_at', desc=True).execute().data or []
        food_list = client.table('pet_food_guide').select('*').order('id').execute().data or []
    except Exception as e:
        print(f"Admin Data Error: {e}")
        users = [];
        pets = [];
        families = [];
        members = [];
        pet_owners_data = [];
        updates_list = [];
        reg_codes = [];
        food_list = []
    # 2. 建立基础映射字典 (ID -> Name)
    fam_map = {f['id']: f['name'] for f in families}
    user_name_map = {u['id']: u['display_name'] for u in users}

    # 3. 处理家庭成员概况 (计算人数 + 列出前几名成员)
    # 结构: { family_id: ["张三", "李四"] }
    fam_members_list = {}
    for m in members:
        fid = m['family_id']
        uid = m['user_id']
        if fid not in fam_members_list: fam_members_list[fid] = []
        # 如果用户存在，加入列表
        if uid in user_name_map:
            fam_members_list[fid].append(user_name_map[uid])

    for f in families:
        mems = fam_members_list.get(f['id'], [])
        f['member_count'] = len(mems)
        f['members_str'] = "、".join(mems[:5]) + ("..." if len(mems) > 5 else "") if mems else "暂无成员"

    # 4. 处理用户归属 (一个用户可能属于多个家庭)
    # 结构: { user_id: ["家庭A", "家庭B"] }
    user_fam_map = {}
    for m in members:
        uid = m['user_id']
        fid = m['family_id']
        if fid in fam_map:
            if uid not in user_fam_map: user_fam_map[uid] = []
            # 这里存入字典，包含 ID 和 Name
            user_fam_map[uid].append({'id': fid, 'name': fam_map[fid]})

    for u in users:
        # 把列表直接赋给 user，如果为空则设为 []
        u['families_data'] = user_fam_map.get(u['id'], [])

    # 5. 处理宠物信息 (显示家庭 + 显示所有主人)
    # 5.1 构建 { pet_id: ["主人A", "主人B"] }
    pet_owners_map = {}
    for po in pet_owners_data:
        pid = po['pet_id']
        uid = po['user_id']
        if pid not in pet_owners_map: pet_owners_map[pid] = []
        if uid in user_name_map:
            pet_owners_map[pid].append(user_name_map[uid])

    # 5.2 回填给 pets
    for p in pets:
        # 填家庭名
        p['family_name'] = fam_map.get(p['family_id'], '🚫 流浪中')
        # 填主人名
        owners = pet_owners_map.get(p['id'], [])
        p['owners_str'] = "、".join(owners) if owners else "无主"

    # 6. 文件存储分析 (查找上传者)
    storage_files = []
    total_size = 0
    storage_breakdown = {'pet': 0, 'moment': 0, 'avatar': 0, 'inventory': 0, 'other': 0}
    if admin_supabase:
        try:
            file_owner = {}
            # 1. 宠物图
            logs = client.table('logs').select('image_path, user_id').neq('image_path', 'null').execute().data
            for l in logs:
                name = user_name_map.get(l['user_id'], '未知')
                file_owner[l['image_path']] = f"{name} (宠物)"

            # 2. 动态图
            moms = client.table('moments').select('image_path, user_id').neq('image_path', 'null').execute().data
            for m in moms:
                name = user_name_map.get(m['user_id'], '未知')
                file_owner[m['image_path']] = f"{name} (动态)"

            # 3. 头像
            for u in users:
                if u.get('avatar_url'):
                    name = u['display_name']
                    file_owner[u['avatar_url']] = f"{name} (头像)"

            # 4. 收纳图
            invs = client.table('family_inventory').select('image_path, created_by').neq('image_path',
                                                                                         'null').execute().data
            for i in invs:
                name = user_name_map.get(i['created_by'], '未知')
                file_owner[i['image_path']] = f"{name} (收纳)"

            # 遍历文件列表
            # [修改] 显式指定路径为根目录 '/'，并忽略空文件夹占位符

            print("正在尝试列出文件...")
            files = client.storage.from_("family_photos").list(path="")
            print(f"DEBUG: 找到了 {len(files)} 个文件")
            print(f"DEBUG: 文件列表: {files}")
            for f in files:
                name = f['name']
                if name == '.emptyFolderPlaceholder': continue

                # [修复] 强制把大小转为整数，防止 MemFire 返回字符串导致报错
                try:
                    size = int(f.get('metadata', {}).get('size', 0))
                except:
                    size = 0

                total_size += size
                # [新增] 分类统计逻辑
                if name.startswith('pet_'):
                    storage_breakdown['pet'] += size
                elif name.startswith('moment_'):
                    storage_breakdown['moment'] += size
                elif name.startswith('avatar_'):
                    storage_breakdown['avatar'] += size
                elif name.startswith('inv_'):
                    storage_breakdown['inventory'] += size
                else:
                    storage_breakdown['other'] += size

                raw_time = f.get('created_at', '')
                fmt_time = raw_time
                try:
                    if raw_time:
                        # 1. 解析字符串为时间对象 (处理结尾的 Z)
                        if raw_time.endswith('Z'):
                            dt_utc = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
                        else:
                            dt_utc = datetime.fromisoformat(raw_time)

                        # 2. 转为北京时间 (UTC+8)
                        dt_bj = dt_utc.astimezone(timezone(timedelta(hours=8)))

                        # 3. 格式化为字符串
                        fmt_time = dt_bj.strftime('%Y-%m-%d %H:%M:%S')
                except Exception as e:
                    # 如果解析失败，回退到简单截取
                    fmt_time = raw_time[:19].replace('T', ' ')

                uploader = file_owner.get(name)
                uploader_str = f"✅ {uploader}" if uploader else '⚠️ 无记录'

                storage_files.append({
                    "name": name,
                    "size_kb": round(size / 1024, 2),
                    "created_at_fmt": fmt_time,
                    "url": client.storage.from_("family_photos").get_public_url(name),
                    "uploader": uploader_str
                })
            storage_files.sort(key=lambda x: x['created_at_fmt'], reverse=True)
        except Exception as e:
            print(f"❌ 存储查询报错: {e}")

    # 7. Auth 用户 (Supabase 底层账户)
    auth_users = []
    if admin_supabase:
        try:
            r = admin_supabase.auth.admin.list_users()
            ul = r if isinstance(r, list) else getattr(r, 'users', [])
            for u in ul:
                auth_users.append({
                    "id": u.id,
                    "email": u.email,
                    "created_at": str(u.created_at)[:19]
                })
        except:
            pass

    # 8. 汇总统计数据
    stats = {
        "users": len(users),
        "pets": len(pets),
        "families": len(families),
        "storage_mb": round(total_size / 1048576, 2),
        "file_count": len(storage_files),
        "storage_breakdown": {k: round(v / 1048576, 2) for k, v in storage_breakdown.items()}
    }
    ai_config = {}
    try:
        cfg = client.table('app_config').select('*').execute().data
        for item in cfg:
            ai_config[item['key']] = item['value']
    except:
        pass

    return render_template('admin.html',
                           users=users,  # 用户列表
                           pets=pets,  # 宠物列表 (含主人信息)
                           families=families,  # 家庭列表 (含人数)
                           files=storage_files,  # 文件列表 (含上传者)
                           stats=stats,  # 顶部统计数字
                           auth_users=auth_users,  # 底层 Auth 用户
                           updates=updates_list,  # 更新日志列表
                           reg_codes=reg_codes,  # [新增] 注册暗号列表
                           user_name=session.get('display_name'),
                           food_list=food_list,
                           ai_config=ai_config)

# 3. 新增 API: 获取服务器实时状态
@app.route('/api/server_stats')
@admin_required
def api_server_stats():
    """实时 CPU 和 内存"""
    try:
        cpu = psutil.cpu_percent(interval=None) # 获取当前CPU百分比
        memory = psutil.virtual_memory()
        return jsonify({
            'cpu': cpu,
            'memory': memory.percent,
            'memory_used': round(memory.used / 1024 / 1024, 1), # MB
            'memory_total': round(memory.total / 1024 / 1024, 1) # MB
        })
    except:
        return jsonify({'cpu': 0, 'memory': 0})

@app.route('/admin/login_as/<uid>')
@admin_required
def admin_login_as(uid):
    if not ENABLE_GOD_MODE:
        flash("为了隐私安全，上帝模式已禁用。", "warning")
        return redirect(url_for('admin_dashboard'))
    """
    [关键功能] 上帝模式：管理员代登录
    """
    if not admin_supabase:
        flash("未配置 Service Key，无法使用代登录", "danger")
        return redirect(url_for('admin_dashboard'))

    try:
        # 获取目标用户信息
        target_profile = admin_supabase.table('profiles').select("*").eq('id', uid).single().execute()
        target_auth = admin_supabase.auth.admin.get_user_by_id(uid)

        if target_profile.data and target_auth.user:
            # 清除管理员自身的 Session
            session.clear()

            # 伪造 Session
            session['user'] = target_profile.data['id']
            session['display_name'] = target_profile.data['display_name']
            session['email'] = target_auth.user.email

            # [核心] 设置标记，告诉 get_db() 这是一个伪装请求，使用 Service Key
            session['is_impersonator'] = True

            flash(f"🚀 已切换身份为: {session['display_name']} (上帝模式)", "warning")
            return redirect(url_for('home'))
        else:
            flash("找不到该用户", "danger")
    except Exception as e:
        flash(f"代登录失败: {e}", "danger")

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/reset_password/<uid>', methods=['POST'])
@admin_required
def admin_reset_password(uid):
    """重置用户密码为 123456"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))
    try:
        admin_supabase.auth.admin.update_user_by_id(uid, {"password": "123456"})
        flash("✅ 密码已重置为: 123456", "success")
    except Exception as e:
        flash(f"重置失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_user/<uid>', methods=['POST'])
@admin_required
def admin_delete_user(uid):
    """彻底删除用户"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))
    try:
        # 级联删除数据 (虽然数据库设置了 cascade，但手动删更保险)
        admin_supabase.table('moments').delete().eq('user_id', uid).execute()
        admin_supabase.table('logs').delete().eq('user_id', uid).execute()
        admin_supabase.table('profiles').delete().eq('id', uid).execute()
        admin_supabase.auth.admin.delete_user(uid)
        flash("用户及其数据已清除", "success")
    except Exception as e:
        flash(f"删除失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/add_pet', methods=['POST'])
@admin_required
def admin_add_pet():
    """管理员添加宠物 (可指定家庭ID，这里暂未做UI，默认为NULL)"""
    name = request.form.get('name')
    type_ = request.form.get('type')
    client = admin_supabase if admin_supabase else supabase
    if name and type_:
        try:
            client.table('pets').insert({"name": name, "type": type_}).execute()
            flash(f"宠物 {name} 添加成功 (注意：需要手动分配家庭ID)", "success")
        except Exception as e:
            flash(f"添加失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_pet/<int:pet_id>', methods=['POST'])
@admin_required
def admin_delete_pet(pet_id):
    """管理员删除宠物"""
    client = admin_supabase if admin_supabase else supabase
    try:
        client.table('logs').delete().eq('pet_id', pet_id).execute()
        client.table('pets').delete().eq('id', pet_id).execute()
        flash("宠物已删除", "warning")
    except Exception as e:
        flash(f"删除失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_file', methods=['POST'])
@admin_required
def admin_delete_file():
    """管理员删除文件"""
    file_name = request.form.get('file_name')
    if file_name:
        try:
            supabase.storage.from_("family_photos").remove(file_name)
            flash("文件已删除", "success")
        except Exception as e:
            flash(f"删除失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/add_family', methods=['POST'])
@admin_required
def admin_add_family():
    name = request.form.get('name')
    if name:
        code = generate_invite_code()
        client = admin_supabase if admin_supabase else supabase
        try:
            # ✅ 修正：直接 execute()
            client.table('families').insert({"name": name, "invite_code": code}).execute()
            flash(f"家庭 [{name}] 创建成功，邀请码: {code}", "success")
        except Exception as e:
            flash(f"创建失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_family/<int:fid>', methods=['POST'])
@admin_required
def admin_delete_family(fid):
    """管理员解散家庭"""
    try:
        client = admin_supabase if admin_supabase else supabase
        # 先把人踢出来
        client.table('profiles').update({'family_id': None}).eq('family_id', fid).execute()
        client.table('families').delete().eq('id', fid).execute()
        flash("家庭已解散", "warning")
    except Exception as e:
        flash(f"删除失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/unbind_family', methods=['POST'])  # 注意：这里去掉了URL里的<uid>
@admin_required
def admin_unbind_family():
    """管理员踢人 (指定将某人从某家庭移除)"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))

    user_id = request.form.get('user_id')
    family_id = request.form.get('family_id')

    try:
        # 从中间表删除记录
        admin_supabase.table('family_members').delete() \
            .eq('user_id', user_id) \
            .eq('family_id', family_id) \
            .execute()
        flash("已将该用户移出指定家庭", "success")
    except Exception as e:
        flash(f"解绑失败: {str(e)}", "danger")

    return redirect(url_for('admin_dashboard'))


# ================= 急救维修通道 =================
@app.route('/fix_cookie')
def fix_cookie():
    """
    当出现 500 错误或 CSRF 报错无法进入时，
    在浏览器地址栏手动输入 /fix_cookie 来强制清空所有残留
    """
    response = redirect(url_for('login'))

    # 1. 清空服务端 Session
    session.clear()

    # 2. 强制过期客户端 Cookie (核心修复)
    # 这里的 'session' 是 Flask 默认的 cookie 名，如果你没改配置的话
    response.delete_cookie('session')

    # 3. 以防万一，把 domain 相关的也清一下
    response.set_cookie('session', '', expires=0)

    return response


@app.route('/create_pet', methods=['POST'])
@login_required
def create_pet():
    db = get_db()
    name = request.form.get('name')
    type_ = request.form.get('type')
    family_id = request.form.get('family_id')

    if not name or not family_id:
        flash("请填写完整信息", "warning")
        return redirect(url_for('home', tab='pets'))

    try:
        # 1. 插入宠物表
        res = db.table('pets').insert({
            "name": name,
            "type": type_,
            "family_id": family_id
        }).execute()

        if res.data:
            new_pet_id = res.data[0]['id']
            # 2. 插入主人表 (登记房产证)
            db.table('pet_owners').insert({
                "pet_id": new_pet_id,
                "user_id": session['user']
            }).execute()
            flash(f"萌宠 {name} 驾到！", "success")
        else:
            flash("添加失败", "danger")

    except Exception as e:
        flash(f"添加失败: {e}", "danger")

    return redirect(url_for('home', tab='pets'))


# --- 3. 新增：修改宠物信息 (仅主人) ---
@app.route('/update_pet', methods=['POST'])
@login_required
def update_pet():
    db = get_db()
    pet_id = request.form.get('pet_id')
    name = request.form.get('name')
    # 处理删除逻辑
    if request.form.get('action') == 'delete':
        try:
            # 级联删除日志等 (数据库设置了cascade，但Storage图片没删，这里简单处理)
            # 只要 RLS 通过，就能删
            db.table('pets').delete().eq('id', pet_id).execute()
            flash("宠物已送养 (删除)", "warning")
        except Exception as e:
            flash(f"删除失败 (可能不是主人): {e}", "danger")
        return redirect(url_for('home', tab='pets'))

    # 处理修改逻辑
    if name:
        try:
            db.table('pets').update({"name": name}).eq('id', pet_id).execute()
            flash("信息已更新", "success")
        except Exception as e:
            flash(f"更新失败 (可能不是主人): {e}", "danger")

    return redirect(url_for('home', tab='pets'))


# --- 4. 新增：添加共管主人 ---
@app.route('/add_pet_owner', methods=['POST'])
@login_required
def add_pet_owner():
    db = get_db()
    pet_id = request.form.get('pet_id')
    new_owner_id = request.form.get('new_owner_id')

    if not pet_id or not new_owner_id:
        flash("参数错误", "warning")
        return redirect(url_for('home', tab='pets'))

    try:
        # 直接插入，RLS 会检查你是不是有权限（即你是不是现任主人）
        db.table('pet_owners').insert({
            "pet_id": pet_id,
            "user_id": new_owner_id
        }).execute()
        flash("成功添加共管主人！", "success")
    except Exception as e:
        # 如果重复添加会报错
        if "duplicate" in str(e):
            flash("他/她已经是主人了", "info")
        else:
            flash(f"添加失败 (你可能不是主人): {e}", "danger")

    return redirect(url_for('home', tab='pets'))


@app.route('/admin/publish_update', methods=['POST'])
@admin_required
def admin_publish_update():
    """发布更新日志"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))

    version = request.form.get('version')
    content = request.form.get('content')
    is_pushed = request.form.get('is_pushed') == 'on'  # Checkbox 返回 'on' 或 None

    if version and content:
        try:
            # 如果设为推送，先把其他的都设为不推送 (保证只有一个弹窗)
            if is_pushed:
                admin_supabase.table('app_updates').update({'is_pushed': False}).neq('id', -1).execute()

            admin_supabase.table('app_updates').insert({
                'version': version,
                'content': content,
                'is_pushed': is_pushed
            }).execute()
            flash(f"版本 v{version} 发布成功！", "success")
        except Exception as e:
            flash(f"发布失败: {e}", "danger")

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_update/<int:uid>', methods=['POST'])
@admin_required
def admin_delete_update(uid):
    """删除日志"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))
    try:
        admin_supabase.table('app_updates').delete().eq('id', uid).execute()
        flash("日志已删除", "success")
    except:
        flash("删除失败", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/toggle_update_status/<int:uid>', methods=['POST'])
@admin_required
def admin_toggle_update_status(uid):
    """切换公告推送状态"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))

    try:
        # 1. 先查当前状态
        target = admin_supabase.table('app_updates').select('is_pushed').eq('id', uid).single().execute()
        if target.data:
            current_status = target.data['is_pushed']
            new_status = not current_status

            # 2. 如果要开启(True)，为了防止首页弹多个窗，先把其他的全关掉
            if new_status:
                admin_supabase.table('app_updates').update({'is_pushed': False}).neq('id', -1).execute()

            # 3. 更新当前这一条
            admin_supabase.table('app_updates').update({'is_pushed': new_status}).eq('id', uid).execute()

            status_text = "已开启推送" if new_status else "已关闭推送"
            flash(f"操作成功: {status_text}", "success")
    except Exception as e:
        flash(f"操作失败: {e}", "danger")

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/generate_reg_code', methods=['POST'])
@admin_required
def admin_generate_reg_code():
    """生成新的注册暗号"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))
    try:
        # 生成6位纯数字 (方便输入)
        new_code = ''.join(random.choices(string.digits, k=6))
        max_uses = int(request.form.get('max_uses', 3))

        admin_supabase.table('registration_codes').insert({
            'code': new_code,
            'max_uses': max_uses,
            'created_by': session['user']
        }).execute()
        flash(f"新暗号 {new_code} 生成成功 (可用 {max_uses} 次)", "success")
    except Exception as e:
        flash(f"生成失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_reg_code/<int:cid>', methods=['POST'])
@admin_required
def admin_delete_reg_code(cid):
    """删除/作废暗号"""
    try:
        admin_supabase.table('registration_codes').delete().eq('id', cid).execute()
        flash("暗号已作废", "success")
    except:
        flash("操作失败", "danger")
    return redirect(url_for('admin_dashboard'))


@app.route('/add_wish', methods=['POST'])
@login_required
def add_wish():
    """许愿点菜"""
    db = get_db()
    family_id = request.form.get('family_id')
    content = request.form.get('content')

    if content:
        try:
            db.table('family_wishes').insert({
                'family_id': family_id,
                'content': content,
                'created_by': session['user']
            }).execute()
            # [新增] 微信推送
            who = session.get('display_name', '家人')
            send_wechat_push(
                family_id=family_id,  # 直接传当前操作的 family_id
                summary=f"🍽️ {who} 想吃：{content}",
                content=f"{who} 点菜啦..."
            )
            flash("许愿成功！坐等开饭~", "success")
        except Exception as e:
            flash(f"许愿失败: {e}", "danger")

    return redirect(url_for('home'))


@app.route('/operate_wish', methods=['POST'])
@login_required
def operate_wish():
    """操作菜单: 变状态 / 删除"""
    db = get_db()
    wish_id = request.form.get('wish_id')
    action = request.form.get('action')
    current_status = request.form.get('current_status')

    try:
        if action == 'delete':
            db.table('family_wishes').delete().eq('id', wish_id).execute()
            flash("已删除该菜品", "info")

        elif action == 'next_status':
            # 状态流转: wanted -> bought -> eaten -> wanted
            new_status = 'bought'
            if current_status == 'bought':
                new_status = 'eaten'
            elif current_status == 'eaten':
                new_status = 'wanted'

            db.table('family_wishes').update({'status': new_status}).eq('id', wish_id).execute()

            # [修改] 微信推送逻辑
            if new_status == 'bought':
                who = session.get('display_name', '家人')

                # 1. [关键修改] 查询菜名的同时，把 family_id 也查出来
                wish_res = db.table('family_wishes').select('content, family_id').eq('id', wish_id).single().execute()

                if wish_res.data:
                    dish_name = wish_res.data['content']
                    target_family_id = wish_res.data['family_id']  # 拿到家庭ID了！

                    # 2. 发送推送
                    send_wechat_push(
                        family_id=target_family_id,
                        summary=f"🛒 {who} 接单了：{dish_name}",
                        content=f"好消息！{who} 已经把【{dish_name}】安排上了！\n坐等开饭吧~"
                    )

    except Exception as e:
        flash(f"操作失败: {e}", "danger")

    return redirect(url_for('home'))


@app.route('/update_status', methods=['POST'])
@login_required
def update_status():
    """切换我的状态"""
    db = get_db()
    new_status = request.form.get('status')

    if new_status:
        try:
            db.table('profiles').update({'status': new_status}).eq('id', session['user']).execute()
            # 不用 flash 提示，前端自动变就好，减少打扰
        except Exception as e:
            print(f"Status Update Error: {e}")

    return redirect(url_for('home'))


@app.route('/nudge_member', methods=['POST'])
@login_required
def nudge_member():
    """拍一拍家人 (带数据记录)"""
    db = get_db()
    target_uid = request.form.get('target_uid')
    target_name = request.form.get('target_name')
    family_id = request.form.get('family_id')

    if not target_uid or not family_id: return redirect(url_for('home'))

    try:
        my_name = session.get('display_name', '我')
        msg = f"👋 {my_name} 拍了拍 {target_name}"

        # 1. 写入家庭留言板 (App内显示)
        # [修改] 增加 target_user_id，用于生成亲密引力场
        db.table('family_reminders').insert({
            'family_id': family_id,
            'content': msg,
            'sender_name': '系统',
            'created_by': session['user'],
            'target_user_id': target_uid  # <--- 关键新增
        }).execute()

        # 2. 发送微信推送 (保持不变)
        send_wechat_push(
            family_id=family_id,
            summary=f"👋 {my_name} 拍了拍 {target_name}",
            content=f"家庭里的互动：\n{my_name} 刚刚拍了拍 {target_name} 的脑袋。\n快去App看看吧！"
        )

        flash(f"你拍了拍 {target_name}", "success")
    except Exception as e:
        print(f"Nudge Error: {e}")

    return redirect(url_for('home'))

# [新增] 添加大事记
@app.route('/add_family_event', methods=['POST'])
@login_required
def add_family_event():
    db = get_db()
    try:
        db.table('family_events').insert({
            'family_id': request.form.get('family_id'),
            'title': request.form.get('title'),
            'event_date': request.form.get('event_date'),
            'event_type': request.form.get('event_type'),  # solar/lunar
            'is_repeat': request.form.get('is_repeat') == 'on'  # Checkbox
        }).execute()
        flash("添加成功", "success")
    except Exception as e:
        flash(f"失败: {e}", "danger")
    return redirect(url_for('home'))


# [新增] 删除大事记
@app.route('/delete_family_event', methods=['POST'])
@login_required
def delete_family_event():
    try:
        # 如果类型是 reunion，说明是删旧版倒计时
        if request.form.get('type') == 'reunion':
            get_db().table('families').update({'reunion_date': None, 'reunion_name': None}) \
                .eq('id', request.form.get('family_id')).execute()
        else:
            # 删新表
            get_db().table('family_events').delete().eq('id', request.form.get('event_id')).execute()
        flash("已删除", "success")
    except:
        pass
    return redirect(url_for('home'))


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_server_error(e):
    return render_template('500.html'), 500


# ================= 🐍 贪吃蛇排行榜接口 =================

@app.route('/api/snake/update', methods=['POST'])
@login_required
def update_snake_score():
    """更新最高分"""
    db = get_db()
    try:
        new_score = int(request.json.get('score', 0))
        user_id = session['user']

        # 1. 先查旧分数
        # 使用 maybe_single 防止报错
        res = db.table('profiles').select('snake_high_score').eq('id', user_id).maybe_single().execute()

        old_score = 0
        if res.data and res.data.get('snake_high_score'):
            old_score = res.data['snake_high_score']

        # 2. 只有破纪录才更新
        if new_score > old_score:
            db.table('profiles').update({'snake_high_score': new_score}).eq('id', user_id).execute()
            return jsonify({'success': True, 'new_record': True})

        return jsonify({'success': True, 'new_record': False})

    except Exception as e:
        print(f"Score Update Error: {e}")
        return jsonify({'success': False})


@app.route('/api/snake/leaderboard')
def get_snake_leaderboard():
    """获取全局排行榜 (前20名)"""
    # ⚠️ 关键点：使用 admin_supabase (上帝权限)
    # 因为 RLS 限制了普通用户只能看家人的资料，但排行榜我们想看全员的
    # 我们只取头像、名字、分数，不泄露隐私
    client = admin_supabase if admin_supabase else supabase

    try:
        res = client.table('profiles') \
            .select('display_name, avatar_url, snake_high_score') \
            .gt('snake_high_score', 0) \
            .order('snake_high_score', desc=True) \
            .limit(20) \
            .execute()

        # 处理头像链接
        data = res.data or []
        for p in data:
            if p.get('avatar_url'):
                p['avatar_url'] = f"{url}/storage/v1/object/public/family_photos/{p['avatar_url']}"
            else:
                p['avatar_url'] = None  # 前端处理默认图

        return jsonify(data)
    except Exception as e:
        print(f"Leaderboard Error: {e}")
        return jsonify([])


@app.route('/delete_pet_photo', methods=['POST'])
@login_required
def delete_pet_photo():
    """删除宠物照片 (仅限上传者)"""
    db = get_db()
    log_id = request.form.get('log_id')
    pet_id = request.form.get('pet_id')

    try:
        # 1. 先查询照片信息 (为了拿路径和验证上传者)
        # RLS 策略虽然有保障，但我们在代码里再做一次校验更稳妥
        log_res = db.table('logs').select('*').eq('id', log_id).single().execute()

        if log_res.data:
            record = log_res.data
            # 校验：只有上传者本人可以删
            if record['user_id'] == session['user']:
                # A. 删文件
                if record.get('image_path'):
                    db.storage.from_("family_photos").remove(record['image_path'])

                # B. 删记录
                db.table('logs').delete().eq('id', log_id).execute()
                flash("照片已删除", "success")
            else:
                flash("你不能删除别人上传的照片哦", "warning")
        else:
            flash("照片不存在或已被删除", "info")

    except Exception as e:
        flash(f"删除失败: {e}", "danger")

    return redirect(url_for('pet_detail', pet_id=pet_id))


@app.route('/api/toggle_like', methods=['POST'])
@login_required
def toggle_like():
    """点赞 API (返回头像列表)"""
    db = get_db()
    try:
        data = request.json
        moment_id = data.get('moment_id')
        user_id = session['user']

        # 1. 检查并切换状态
        check = db.table('moment_likes').select('*').eq('user_id', user_id).eq('moment_id', moment_id).execute()

        if check.data:
            db.table('moment_likes').delete().eq('user_id', user_id).eq('moment_id', moment_id).execute()
            is_liked = False
        else:
            db.table('moment_likes').insert({'user_id': user_id, 'moment_id': moment_id}).execute()
            is_liked = True

        # 2. [核心] 获取最新的点赞人列表 (为了前端渲染)
        # 这里需要重新构建一下简单的 user_map 或者直接查 profiles
        # 为了简单，我们只返回 user_id 列表，前端根据页面已有的 user_map 渲染?
        # 不行，前端 user_map 是 Jinja2 渲染的，JS 拿不到完整版。
        # 所以后端直接查好返回给前端最稳妥。

        likers_res = db.table('moment_likes').select('user_id').eq('moment_id', moment_id).execute()
        uids = [x['user_id'] for x in (likers_res.data or [])]

        likers_info = []
        if uids:
            profiles = db.table('profiles').select('id, display_name, avatar_url').in_('id', uids).execute()
            for p in (profiles.data or []):
                avatar = None
                if p.get('avatar_url'):
                    avatar = f"{url}/storage/v1/object/public/family_photos/{p['avatar_url']}"

                likers_info.append({
                    'id': p['id'],
                    'name': p['display_name'],
                    'avatar': avatar
                })

        return jsonify({'success': True, 'is_liked': is_liked, 'likers': likers_info})

    except Exception as e:
        print(f"Like Error: {e}")
        return jsonify({'success': False})


@app.route('/send_game_result', methods=['POST'])
@login_required
def send_game_result():
    """游戏结果通知 (无频率限制)"""
    db = get_db()
    family_id = request.form.get('family_id')
    content = request.form.get('content')

    if not content: return redirect(url_for('home'))

    try:
        # 发送者名字改成 "命运之轮" 或者 "系统" 更有趣
        sender_name = "🎡 命运之轮"

        # 1. 直接插入提醒表 (不查今日是否发过)
        db.table('family_reminders').insert({
            'family_id': family_id,
            'content': content,
            'sender_name': sender_name,
            # created_by 依然记你，但我们不查这个字段做限制
            'created_by': session['user']
        }).execute()

        # 2. 微信推送
        # 先查推送ID
        fam_res = db.table('families').select('wx_topic_id').eq('id', family_id).single().execute()
        # 注意：如果你已经改成了 UID 模式，这里直接调用 send_wechat_push(family_id, ...) 即可
        # 下面按 UID 模式写：
        send_wechat_push(
            family_id=family_id,
            summary=f"🎡 命运大转盘出结果啦！",
            content=f"{content}\n\n(点击查看详情)"
        )

        flash("结果已公示给全家！", "success")
    except Exception as e:
        flash(f"公示失败: {e}", "danger")

    return redirect(url_for('home'))


@app.route('/add_footprint', methods=['POST'])
@login_required
def add_footprint():
    """添加足迹"""
    db = get_db()
    family_id = request.form.get('family_id')
    city_name = request.form.get('city_name')

    if city_name:
        # 复用之前写好的搜索函数，获取经纬度
        cid, cname, lat, lon = search_city_qweather(city_name)

        if lat and lon:
            try:
                db.table('family_footprints').insert({
                    'family_id': family_id,
                    'city_name': cname,
                    'city_id': cid,
                    'lat': lat,
                    'lon': lon,
                    'created_by': session['user']
                }).execute()
                flash(f"已点亮城市：{cname} ✨", "success")
            except Exception as e:
                flash(f"添加失败: {e}", "danger")
        else:
            flash("找不到该城市，请尝试输入标准名称 (如: 成都)", "warning")

    return redirect(url_for('home'))


@app.route('/delete_footprint', methods=['POST'])
@login_required
def delete_footprint():
    """删除足迹"""
    try:
        get_db().table('family_footprints').delete().eq('id', request.form.get('fp_id')).execute()
        flash("已移除该足迹", "info")
    except:
        pass
    return redirect(url_for('home'))


# ================= 家庭角色卡数据接口 =================

@app.route('/api/family_stats', methods=['POST'])
@login_required
def get_family_stats():
    """获取家庭角色卡 (本周战绩版)"""
    client = admin_supabase if admin_supabase else get_db()

    family_id = request.json.get('family_id')
    if not family_id: return jsonify([])
    # [新增] === 懒加载归档：检查上周是否已结算 ===
    try:
        now = datetime.now(timezone(timedelta(hours=8)))
        # 获取上周的年份和周数 (ISO标准)
        last_week_date = now - timedelta(days=7)
        year, week, _ = last_week_date.isocalendar()
        week_str = f"{year}-W{week}"

        # 查库：上周结算过吗？
        check = client.table('family_weekly_honors').select('id').eq('family_id', family_id).eq('week_str',
                                                                                                week_str).execute()

        if not check.data:
            # 没结算 -> 开始补算上周数据
            # 上周一 00:00 ~ 本周一 00:00
            this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            last_monday = this_monday - timedelta(days=7)

            t_start = last_monday.astimezone(timezone.utc).isoformat()
            t_end = this_monday.astimezone(timezone.utc).isoformat()

            # 调用刚才写的计算函数
            winner = calculate_champion(client, family_id, t_start, t_end)

            if winner:
                # 存入荣誉表
                client.table('family_weekly_honors').insert({
                    'family_id': family_id,
                    'week_str': week_str,
                    'winner_id': winner['uid'],
                    'title': winner['title'],
                    'score_data': {'total': winner['score']}
                }).execute()
                print(f"✅ 已自动归档上周 ({week_str}) 冠军")
            else:
                # 上周没人互动，插个空记录防止重复计算
                pass
    except Exception as e:
        print(f"Archive Error: {e}")

    try:
        # 1. 计算"本周一 00:00"的 UTC 时间 (用于过滤数据)
        now = datetime.now(timezone(timedelta(hours=8)))  # 北京时间
        # 找到本周一 (weekday: 0=Mon, 6=Sun)
        start_of_week = now - timedelta(days=now.weekday())
        start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        # 转为 UTC 字符串供数据库查询
        filter_time = start_of_week.astimezone(timezone.utc).isoformat()

        # 2. 获取成员
        mems = client.table('family_members').select('user_id, created_at').eq('family_id', family_id).execute()
        member_list = mems.data or []
        if not member_list: return jsonify([])

        user_ids = [m['user_id'] for m in member_list]
        profiles = client.table('profiles').select('id, display_name, avatar_url').in_('id', user_ids).execute()
        user_info_map = {p['id']: p for p in (profiles.data or [])}

        # 3. 初始化计数器
        stats = {uid: {'guardian': 0, 'recorder': 0, 'foodie': 0, 'care': 0, 'seniority': 1} for uid in user_ids}

        # A. 守护力 (本周喂食/遛狗)
        pets = client.table('pets').select('id').eq('family_id', family_id).execute()
        pet_ids = [p['id'] for p in pets.data] if pets.data else []
        if pet_ids:
            logs = client.table('logs').select('user_id') \
                .in_('pet_id', pet_ids) \
                .gte('created_at', filter_time) \
                .execute()
            for l in (logs.data or []):
                if l['user_id'] in stats: stats[l['user_id']]['guardian'] += 1

        # B. [修复] 记录力 (本周动态：公开 + 本家庭)
        # 逻辑：(target is null OR target = family_id) AND created_at >= 本周
        moms = client.table('moments').select('user_id') \
            .or_(f"target_family_id.is.null,target_family_id.eq.{family_id}") \
            .gte('created_at', filter_time) \
            .execute()

        for m in (moms.data or []):
            uid = m['user_id']
            # 只有当发动态的人在当前家庭成员列表里，才统计
            if uid in stats:
                stats[uid]['recorder'] += 1

        # C. 美食魂 (本周许愿)
        wishes = client.table('family_wishes').select('created_by') \
            .eq('family_id', family_id) \
            .gte('created_at', filter_time) \
            .execute()
        for w in (wishes.data or []):
            if w['created_by'] in stats: stats[w['created_by']]['foodie'] += 1

        # D. 关怀力 (本周提醒)
        rems = client.table('family_reminders').select('created_by') \
            .eq('family_id', family_id) \
            .gte('created_at', filter_time) \
            .execute()
        for r in (rems.data or []):
            if r['created_by'] in stats: stats[r['created_by']]['care'] += 1

        # E. 元老值 (累计天数，不按周算，这是资历)
        now_date = datetime.now(timezone(timedelta(hours=8))).date()

        for m in member_list:
            uid = m['user_id']
            if uid in stats:
                try:
                    # [暴力修复] 不解析时区，直接截取字符串前10位 (YYYY-MM-DD)
                    # 数据库格式不管是 "2025-12-05T..." 还是 "2025-12-05 15:..."，前10位永远是日期
                    raw_time = str(m['created_at'])
                    date_str = raw_time[:10]

                    # 转为日期对象
                    join_date = datetime.strptime(date_str, '%Y-%m-%d').date()

                    # 计算天数
                    days = (now_date - join_date).days

                    # 修正：最少算1天
                    final_days = max(1, days + 1)

                    stats[uid]['seniority'] = final_days

                    # [调试日志] 如果是 0 或 1，打印出来看看
                    if final_days <= 1:
                        print(f"DEBUG Seniority: UID={uid}, Raw={raw_time}, Calc={final_days}")

                except Exception as e:
                    # 万一报错，打印出来，并给个保底值 1
                    print(f"❌ 元老值计算失败: {e} (Raw: {m.get('created_at')})")
                    stats[uid]['seniority'] = 1

        # 4. 组装返回
        result = []
        for uid, s in stats.items():
            info = user_info_map.get(uid, {})

            # [修改] 称号计算 (按你的新文案)
            # 权重调整：元老值除以 30 (一个月抵一次本周互动，避免老用户躺赢)
            # 其他按次数 1:1 比拼
            scores = {
                '🛡️ 金牌铲屎官': s['guardian'],
                '📸 朋友圈战神': s['recorder'],
                '😋 干饭王': s['foodie'],
                '❤️ 贴心小棉袄': s['care'],
                '🌟 一家之主': s['seniority'] / 30
            }
            title = max(scores, key=scores.get)

            # 如果本周完全没互动 (且元老值权重也没超过0.5)，给个"潜水中"
            # (这里稍微放宽一点，让元老至少有点牌面)
            if all(v == 0 for k, v in scores.items() if k != '🌟 一家之主') and scores['🌟 一家之主'] < 1:
                title = "💤 本周潜水中"

            avatar = None
            if info.get('avatar_url'):
                avatar = f"{url}/storage/v1/object/public/family_photos/{info['avatar_url']}"

            result.append({
                'id': uid,
                'name': info.get('display_name', '家人'),
                'avatar': avatar,
                'title': title,
                'data': [s['guardian'], s['recorder'], s['foodie'], s['care'], s['seniority']]
            })

        return jsonify(result)

    except Exception as e:
        print(f"Stats Error: {e}")
        return jsonify([])


@app.route('/api/family_history', methods=['POST'])
@login_required
def get_family_history():
    """获取往期周榜 (带具体日期计算)"""
    client = admin_supabase if admin_supabase else get_db()
    fid = request.json.get('family_id')
    try:
        check = get_db().table('family_members').select('id') \
            .eq('family_id', fid) \
            .eq('user_id', session['user']) \
            .execute()

        if not check.data:
            # 如果查不到我是成员，直接拒绝
            return jsonify([])
    except:
        return jsonify([])

    try:
        res = client.table('family_weekly_honors') \
            .select('week_str, title, winner_id') \
            .eq('family_id', fid) \
            .order('week_str', desc=True) \
            .limit(10) \
            .execute()

        data = res.data or []
        result = []

        for item in data:
            uid = item['winner_id']
            # 获取用户信息
            p = client.table('profiles').select('display_name, avatar_url').eq('id', uid).single().execute()

            # [核心修改] 计算具体日期范围
            # week_str 格式: "2025-W51"
            date_range_str = ""
            week_num = ""
            try:
                year_str, week_str = item['week_str'].split('-W')
                year = int(year_str)
                week = int(week_str)
                week_num = f"第{week}周"

                # 计算周一和周日
                # fromisocalendar(year, week, day) 1=Monday
                start_date = datetime.fromisocalendar(year, week, 1)
                end_date = start_date + timedelta(days=6)

                # 格式化: 12.15 - 12.21
                date_range_str = f"{start_date.strftime('%m.%d')} - {end_date.strftime('%m.%d')}"
            except:
                date_range_str = item['week_str']  # 算错了就显示原样

            if p.data:
                avatar = None
                if p.data.get('avatar_url'):
                    avatar = f"{url}/storage/v1/object/public/family_photos/{p.data['avatar_url']}"

                result.append({
                    'date_range': date_range_str,  # 如: 12.15 - 12.21
                    'week_num': week_num,  # 如: 第51周
                    'title': item['title'],
                    'name': p.data['display_name'],
                    'avatar': avatar
                })
        return jsonify(result)
    except Exception as e:
        print(f"History Error: {e}")
        return jsonify([])


# ================= 🕸️ 亲密引力场接口 =================

@app.route('/api/family_graph', methods=['POST'])
@login_required
def get_family_graph():
    """
    亲密引力场 (逻辑修正版)
    1. 拍一拍：只统计 "👋" 开头的真实互动，排除系统通知。
    2. 兑换券：Active(+3), Used(+5), Void(-2 扣分)。
    """
    client = admin_supabase if admin_supabase else get_db()
    family_id = request.json.get('family_id')
    if not family_id: return jsonify({})

    try:
        # === 1. 获取节点 (Nodes) ===
        mems = client.table('family_members').select('user_id').eq('family_id', family_id).execute()
        user_ids = [m['user_id'] for m in (mems.data or [])]
        if not user_ids: return jsonify({})

        profiles = client.table('profiles').select('id, display_name, avatar_url').in_('id', user_ids).execute()

        nodes = []
        user_map = {}

        for p in (profiles.data or []):
            avatar = "/static/icon.png"
            if p.get('avatar_url'):
                avatar = f"{url}/storage/v1/object/public/family_photos/{p['avatar_url']}"
            user_map[p['id']] = p['display_name']

            nodes.append({
                'id': p['id'],
                'name': p['display_name'],
                'symbol': f'image://{avatar}',
                'symbolSize': 60,
                'itemStyle': {'borderWidth': 2, 'borderColor': '#fff'},
                'value': 0
            })

        # === 2. 计算亲密度 (Links) ===
        # 使用 defaultdict 方便计算，默认值 0
        from collections import defaultdict
        interaction_counts = defaultdict(int)

        # --- A. 统计点赞 (Likes) [+1] ---
        moms = client.table('moments').select('id, user_id') \
            .or_(f"target_family_id.is.null,target_family_id.eq.{family_id}") \
            .execute()
        mom_list = moms.data or []
        mom_author_map = {m['id']: m['user_id'] for m in mom_list}
        all_mom_ids = list(mom_author_map.keys())

        if all_mom_ids:
            chunk_size = 100
            for i in range(0, len(all_mom_ids), chunk_size):
                chunk = all_mom_ids[i:i + chunk_size]
                likes = client.table('moment_likes').select('user_id, moment_id').in_('moment_id', chunk).execute()

                for l in (likes.data or []):
                    liker = l['user_id']
                    author = mom_author_map.get(l['moment_id'])
                    if author and liker != author and liker in user_map and author in user_map:
                        key = f"{liker}|{author}"
                        interaction_counts[key] += 1

        # --- B. 统计拍一拍 (Reminders) [+2] ---
        # [修改] 必须查 content，用来过滤
        rems = client.table('family_reminders') \
            .select('created_by, target_user_id, content') \
            .eq('family_id', family_id) \
            .execute()

        for r in (rems.data or []):
            sender = r.get('created_by')
            target = r.get('target_user_id')
            content = r.get('content', '')

            # [核心修复] 只统计包含 "👋" (拍一拍) 的记录
            # 过滤掉系统自动发的 "🎟️ 发券"、"🚫 作废" 等通知
            if sender and target and sender != target and sender in user_map and target in user_map:
                if '👋' in content:
                    key = f"{sender}|{target}"
                    interaction_counts[key] += 2

        # --- C. 统计兑换券 (Coupons) [分级计分] ---
        # [修改] 必须查 status
        coupons = client.table('family_coupons') \
            .select('creator_id, target_user_id, status') \
            .eq('family_id', family_id) \
            .execute()

        for c in (coupons.data or []):
            sender = c.get('creator_id')
            target = c.get('target_user_id')
            status = c.get('status')

            if sender and target and sender != target and sender in user_map and target in user_map:
                key = f"{sender}|{target}"

                # [核心修复] 根据状态加减分
                if status == 'active':
                    interaction_counts[key] += 3  # 发了券还没用
                elif status == 'used':
                    interaction_counts[key] += 5  # 完美兑现 (分最高)
                elif status == 'void':
                    interaction_counts[key] -= 2  # 作废了 (扣分!)

        # === 3. 生成连线数据 ===
        links = []
        for key, count in interaction_counts.items():
            # 如果扣分扣到 <= 0，就不显示连线了 (或者显示很细的线)
            if count <= 0: continue

            u1, u2 = key.split('|')
            links.append({
                'source': u1,
                'target': u2,
                'value': count,
                'lineStyle': {
                    'width': 1 + min(count, 20) * 0.5,
                    'curveness': 0.2,
                    'opacity': 0.6 + min(count, 30) * 0.01
                }
            })

        return jsonify({'nodes': nodes, 'links': links})

    except Exception as e:
        print(f"Graph Error: {e}")
        return jsonify({'nodes': [], 'links': []})
# ================= 工具箱路由 =================

@app.route('/add_wifi', methods=['POST'])
@login_required
def add_wifi():
    db = get_db()
    try:
        db.table('family_wifis').insert({
            'family_id': request.form.get('family_id'),
            'location': request.form.get('location'),
            'ssid': request.form.get('ssid'),
            'password': request.form.get('password')
        }).execute()
        flash("Wi-Fi 添加成功", "success")
    except Exception as e:
        flash(f"添加失败: {e}", "danger")
    return redirect(url_for('home'))

@app.route('/delete_wifi', methods=['POST'])
@login_required
def delete_wifi():
    try:
        get_db().table('family_wifis').delete().eq('id', request.form.get('id')).execute()
        flash("已删除", "success")
    except: pass
    return redirect(url_for('home'))


@app.route('/add_memo', methods=['POST'])
@login_required
def add_memo():
    db = get_db()
    content = request.form.get('content')

    # [修改] 加密内容
    safe_content = encrypt_data(content)

    try:
        db.table('family_memos').insert({
            'family_id': request.form.get('family_id'),
            'title': request.form.get('title'),
            'content': safe_content  # 存入乱码
        }).execute()
        flash("备忘录保存成功 (已加密)", "success")
    except Exception as e:
        flash(f"添加失败: {e}", "danger")
    return redirect(url_for('home'))

@app.route('/delete_memo', methods=['POST'])
@login_required
def delete_memo():
    try:
        get_db().table('family_memos').delete().eq('id', request.form.get('id')).execute()
        flash("已删除", "success")
    except: pass
    return redirect(url_for('home'))


# ================= 收纳与采购路由 =================

@app.route('/add_inventory', methods=['POST'])
@login_required
def add_inventory():
    """添加收纳物品"""
    db = get_db()
    f = request.files.get('photo')

    data = {
        'family_id': request.form.get('family_id'),
        'item_name': request.form.get('item_name'),
        'location': request.form.get('location'),
        'created_by': session['user']
    }

    if f and f.filename:
        try:
            filename = secure_filename(f.filename)
            file_path = f"inv_{int(datetime.now().timestamp())}_{filename}"
            db.storage.from_("family_photos").upload(file_path, f.read(), {"content-type": f.content_type})
            data['image_path'] = file_path
        except:
            pass

    try:
        db.table('family_inventory').insert(data).execute()
        flash("物品已归档", "success")
    except Exception as e:
        flash(f"添加失败: {e}", "danger")
    return redirect(url_for('home'))


@app.route('/delete_inventory', methods=['POST'])
@login_required
def delete_inventory():
    """删除收纳 (同时删图)"""
    db = get_db()
    inv_id = request.form.get('id')
    try:
        # 1. 先查图片路径
        res = db.table('family_inventory').select('image_path').eq('id', inv_id).single().execute()
        if res.data and res.data.get('image_path'):
            # 2. 删图片
            db.storage.from_("family_photos").remove(res.data['image_path'])

        # 3. 删记录
        db.table('family_inventory').delete().eq('id', inv_id).execute()
        flash("已删除", "success")
    except Exception as e:
        print(f"Del Inv Error: {e}")
    return redirect(url_for('home'))


@app.route('/add_shopping', methods=['POST'])
@login_required
def add_shopping():
    """添加采购项 (支持推送)"""
    db = get_db()
    family_id = request.form.get('family_id')
    content = request.form.get('content')
    notify = request.form.get('notify') == 'on'  # 获取复选框状态

    try:
        db.table('family_shopping_list').insert({
            'family_id': family_id,
            'content': content,
            'created_by': session['user']
        }).execute()
        flash("已添加", "success")

        # [新增] 微信推送
        if notify:
            who = session.get('display_name', '家人')
            send_wechat_push(
                family_id=family_id,
                summary=f"🛒 采购提醒：{content}",
                content=f"{who} 在采购清单里加了：【{content}】\n路过超市记得买哦！"
            )

    except:
        pass
    return redirect(url_for('home'))


@app.route('/toggle_shopping', methods=['POST'])
@login_required
def toggle_shopping():
    """勾选/取消购买"""
    db = get_db()
    item_id = request.form.get('id')
    current_status = request.form.get('status') == 'True'
    try:
        db.table('family_shopping_list').update({'is_bought': not current_status}).eq('id', item_id).execute()
    except:
        pass
    return redirect(url_for('home'))


@app.route('/delete_shopping', methods=['POST'])
@login_required
def delete_shopping():
    """删除采购项"""
    try:
        get_db().table('family_shopping_list').delete().eq('id', request.form.get('id')).execute()
        flash("已删除", "success")
    except:
        pass
    return redirect(url_for('home'))


@app.route('/send_coupon', methods=['POST'])
@login_required
def send_coupon():
    db = get_db()
    family_id = request.form.get('family_id')
    target_uid = request.form.get('target_uid')
    title = request.form.get('title')
    count = int(request.form.get('count', 1))

    if not title or not target_uid: return redirect(url_for('home'))

    try:
        # 1. 发券
        coupons = []
        for _ in range(count):
            coupons.append({
                'family_id': family_id,
                'title': title,
                'creator_id': session['user'],
                'target_user_id': target_uid,
                'status': 'active'
            })
        db.table('family_coupons').insert(coupons).execute()

        # 2. [修改] App 内系统通知 (私密)
        # 写入 reminders 表，但指定 target_user_id
        me = session.get('display_name', '家人')
        db.table('family_reminders').insert({
            'family_id': family_id,
            'content': f"🎟️ {me} 给你发了 {count} 张【{title}】！",
            'sender_name': '系统',
            'created_by': session['user'],
            'target_user_id': target_uid  # <--- 关键：只显示给他看
        }).execute()

        # 3. [修改] 微信私密推送
        send_private_wechat_push(
            target_user_id=target_uid,
            summary=f"🎁 收到 {count} 张兑换券",
            content=f"{me} 给你发了福利：\n券名：{title}\n数量：{count} 张\n\n快去 App 卡包查看吧！"
        )

        flash(f"已发放 {count} 张券", "success")
    except Exception as e:
        print(f"Coupon Error: {e}")
        flash("发放失败", "danger")

    return redirect(url_for('home'))


@app.route('/void_coupon', methods=['POST'])
@login_required
def void_coupon():
    """作废兑换券 (带通知)"""
    db = get_db()
    coupon_id = request.form.get('coupon_id')

    try:
        # 1. 先查详情 (为了拿 title 和 target_user_id)
        check = db.table('family_coupons').select('title, target_user_id, family_id').eq('id',
                                                                                         coupon_id).single().execute()

        if check.data:
            data = check.data
            # 2. 执行作废
            # 只能作废 active 的
            res = db.table('family_coupons').update({'status': 'void'}).eq('id', coupon_id).eq('status',
                                                                                               'active').execute()

            # 如果更新成功 (res.data不为空)，则发送通知
            if res.data:
                target_uid = data['target_user_id']
                family_id = data['family_id']
                title = data['title']
                me = session.get('display_name', '家人')

                # A. App 提醒 (给持有者)
                db.table('family_reminders').insert({
                    'family_id': family_id,
                    'content': f"🚫 {me} 作废了给你的【{title}】",
                    'sender_name': '系统',
                    'created_by': session['user'],
                    'target_user_id': target_uid
                }).execute()

                # B. 微信推送 (给持有者)
                send_private_wechat_push(
                    target_user_id=target_uid,
                    summary=f"🚫 兑换券已作废",
                    content=f"很遗憾，{me} 收回了之前的承诺。\n券名：{title}\n状态：已失效"
                )

                flash("该券已作废，并通知了对方。", "info")
            else:
                flash("操作无效（该券可能已被使用或已作废）", "warning")

    except Exception as e:
        print(f"Void Error: {e}")

    return redirect(url_for('home'))


@app.route('/use_coupon', methods=['POST'])
@login_required
def use_coupon():
    """核销兑换券 (修复并发Bug + 私密通知)"""
    db = get_db()
    coupon_id = request.form.get('coupon_id')
    family_id = request.form.get('family_id')

    try:
        # 1. [核心修复] 先查状态！防止"作废了还能用"
        # 必须同时确认 ID 和 status='active'
        check = db.table('family_coupons').select('status, title, creator_id').eq('id', coupon_id).single().execute()

        if not check.data:
            flash("找不到这张券", "danger")
            return redirect(url_for('home'))

        coupon_data = check.data
        if coupon_data['status'] != 'active':
            flash(f"操作失败：这张券当前状态是【{coupon_data['status']}】，无法使用。", "warning")
            return redirect(url_for('home'))

        # 2. 状态正常，执行核销
        now = datetime.now(timezone.utc).isoformat()
        db.table('family_coupons').update({'status': 'used', 'used_at': now}).eq('id', coupon_id).execute()

        # 3. 通知发行人 (私密)
        creator_id = coupon_data['creator_id']
        title = coupon_data['title']
        user_name = session.get('display_name', '家人')

        # A. 写入 App 内提醒 (指定 target_user_id 为发行人)
        db.table('family_reminders').insert({
            'family_id': family_id,
            'content': f"🎫 {user_name} 使用了【{title}】，请兑现！",
            'sender_name': '系统',
            'created_by': session['user'],
            'target_user_id': creator_id  # 只有发行人能看到
        }).execute()

        # B. 微信推送 (给发行人)
        send_private_wechat_push(
            target_user_id=creator_id,
            summary=f"🆘 {user_name} 使用了券",
            content=f"叮！您的兑换券被使用了！\n使用者：{user_name}\n项目：{title}\n\n请尽快兑现承诺哦！"
        )

        flash("使用成功！已通知对方兑现。", "success")
    except Exception as e:
        flash(f"使用失败: {e}", "danger")

    return redirect(url_for('home'))


# ================= 🤖 AI & 配置模块 =================

def get_sys_config(key_name):
    """获取系统配置"""
    try:
        # 使用 admin 权限查，防止 RLS 意外拦截
        client = admin_supabase if admin_supabase else get_db()
        res = client.table('app_config').select('value').eq('key', key_name).single().execute()
        if res.data:
            return res.data['value']
    except:
        pass
    return ""


@app.route('/admin/update_config', methods=['POST'])
@login_required
@admin_required
def admin_update_config():
    """管理员更新 AI 配置"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))

    configs = {
        'ai_url': request.form.get('ai_url'),
        'ai_key': request.form.get('ai_key'),
        'ai_model': request.form.get('ai_model'),
        'ai_stream': 'true' if request.form.get('ai_stream') == 'on' else 'false'
    }

    try:
        for k, v in configs.items():
            # Upsert: 有则更新，无则插入
            admin_supabase.table('app_config').upsert({'key': k, 'value': v}).execute()
        flash("AI 配置已保存", "success")
    except Exception as e:
        flash(f"保存失败: {e}", "danger")

    return redirect(url_for('admin_dashboard'))


@app.route('/api/ask_vet', methods=['POST'])
@login_required
def ask_vet():
    """AI 兽医接口 (支持流式/非流式切换)"""
    history = request.json.get('history', [])

    api_url = get_sys_config('ai_url')
    api_key = get_sys_config('ai_key')
    model = get_sys_config('ai_model')
    is_stream = get_sys_config('ai_stream') == 'true'  # 读取开关

    if not api_key: return jsonify({'error': '未配置 Key'})

    system_prompt = {"role": "system", "content": "你是一位经验丰富的家庭宠物医生。你的回答必须：1.简洁明了(150字以内)。2.语气温柔但专业。3.对于禁食、中毒等危急情况，必须第一时间建议去医院。4.不要说废话。"}
    messages = [system_prompt] + history

    target_url = api_url.rstrip('/') + "/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {"model": model, "messages": messages, "temperature": 0.7, "stream": is_stream}

    try:
        # === A. 流式模式 (Typewriter) ===
        if is_stream:
            resp = requests.post(target_url, json=payload, headers=headers, stream=True)

            def generate():
                for line in resp.iter_lines():
                    if line:
                        decoded = line.decode('utf-8')
                        if decoded.startswith("data: "):
                            if "[DONE]" in decoded: break
                            try:
                                json_str = decoded[6:]  # 去掉 'data: '
                                chunk = json.loads(json_str)
                                content = chunk['choices'][0]['delta'].get('content', '')
                                if content: yield content
                            except:
                                pass

            return Response(stream_with_context(generate()), content_type='text/plain')

        # === B. 非流式模式 (一次性返回) ===
        else:
            resp = requests.post(target_url, json=payload, headers=headers, timeout=30)
            data = resp.json()
            if 'choices' in data:
                return jsonify({'reply': data['choices'][0]['message']['content']})
            return jsonify({'error': 'API Error'})


    except Exception as e:
        print(f"AI Error: {e}")
        return jsonify({'error': '网络连接超时，请重试'})
@app.route('/api/food_guide')
@login_required
def get_food_guide():
    """获取所有食物禁忌数据"""
    # 允许所有人查，不需要 admin
    try:
        res = get_db().table('pet_food_guide').select('*').order('id').execute()
        return jsonify(res.data or [])
    except: return jsonify([])

@app.route('/admin/add_food', methods=['POST'])
@admin_required
def admin_add_food():
    """后台添加食物"""
    if not admin_supabase: return redirect(url_for('admin_dashboard'))
    try:
        admin_supabase.table('pet_food_guide').insert({
            'name': request.form.get('name'),
            'status': request.form.get('status'),
            'reason': request.form.get('reason')
        }).execute()
        flash("添加成功", "success")
    except Exception as e: flash(f"失败: {e}", "danger")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_food/<int:fid>', methods=['POST'])
@admin_required
def admin_delete_food(fid):
    """后台删除食物"""
    try:
        admin_supabase.table('pet_food_guide').delete().eq('id', fid).execute()
        flash("删除成功", "success")
    except: pass
    return redirect(url_for('admin_dashboard'))
if __name__ == '__main__':
    # 开发环境启动
    app.run(debug=True, host='0.0.0.0', port=5000)
