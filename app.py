import os
import json
import random
import string
from datetime import datetime, timedelta, timezone
from functools import wraps
import requests
from zhdate import ZhDate
# 引入 ProxyFix 修复云端/Nginx反代环境下的 Scheme 问题
from werkzeug.middleware.proxy_fix import ProxyFix
# 引入 Flask 相关组件
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
# 引入 CSRF 保护
from flask_wtf.csrf import CSRFProtect
# Supabase 客户端
from supabase import create_client, Client
# 环境变量加载
from dotenv import load_dotenv
# 文件名安全处理
from werkzeug.utils import secure_filename

# 加载 .env 文件
load_dotenv()

app = Flask(__name__)
CURRENT_APP_VERSION = '3.1.0'
qweather_key = os.environ.get("QWEATHER_KEY")
qweather_host = os.environ.get("QWEATHER_HOST", "https://devapi.qweather.com")
ENABLE_GOD_MODE = False

# ================= 配置区域 =================
# 适配 Vercel/Render 等代理环境，防止 HTTPS 变 HTTP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Secret Key 必须设置，用于 Session 加密和 CSRF
app.secret_key = os.environ.get("SECRET_KEY", "dev_key_must_change_to_something_complex")

# Session 有效期 30 天
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# 限制上传文件最大为 16MB
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

if os.environ.get('VERCEL') == '1' or os.environ.get('FLASK_ENV') == 'production':
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        # [修改] 改回 Lax，兼容性最好，手机不容易报错
        SESSION_COOKIE_SAMESITE='Lax',
        # [修改] 保持 False，防止手机端 Referer 丢失问题
        WTF_CSRF_SSL_STRICT=False
    )
else:
    # 本地开发环境配置
    app.config.update(
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax'
    )

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
        if 'user' not in session: return redirect(url_for('login'))

        # [核心修复] 如果处于上帝模式（已伪装），直接放行，允许进入后台
        if session.get('is_impersonator'):
            return f(*args, **kwargs)

        try:
            # 查权限时使用全局 supabase 即可
            res = supabase.table('profiles').select('role').eq('id', session['user']).single().execute()
            if not res.data or res.data['role'] != 'admin':
                flash("🚫 权限拒绝：你没有管理员权限！", "danger")
                return redirect(url_for('home'))
        except:
            return redirect(url_for('home'))
        return f(*args, **kwargs)

    return decorated_function


@app.context_processor
def inject_version():
    return dict(app_version=CURRENT_APP_VERSION)


# ================= 认证路由 =================
@app.route('/register', methods=['GET', 'POST'])
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
    """主页路由 (集成双城天气版)"""
    current_user_id = session.get('user')
    current_tab = request.args.get('tab', 'pets')
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

                # 查询家庭详情
                if my_family_ids:
                    fams_res = db.table('families').select('*').in_('id', my_family_ids).execute()
                    my_families = fams_res.data or []

                    # 定义基准时间 (北京时间用于倒计时，UTC时间用于缓存判断)
                    bj_now_date = datetime.now(timezone(timedelta(hours=8))).date()
                    utc_now = datetime.now(timezone.utc)

                    for f in my_families:
                        # [全能时间卡片逻辑]
                        f['top_event'] = None
                        candidate_events = []

                        # 1. 归家倒计时 (兼容旧数据)
                        if f.get('reunion_date'):
                            try:
                                target = datetime.strptime(f['reunion_date'], '%Y-%m-%d').date()
                                days = (target - bj_now_date).days
                                if days >= 0:
                                    candidate_events.append({
                                        'title': f.get('reunion_name') or '团圆',
                                        'data': {'days': days, 'total': 0, 'date_str': f['reunion_date'],
                                                 'is_repeat': False},
                                        'type': 'reunion'
                                    })
                            except:
                                pass

                        # 2. 数据库里的家庭大事
                        try:
                            db_events = db.table('family_events').select('*').eq('family_id',
                                                                                 f['id']).execute().data or []
                            for e in db_events:
                                calc = calculate_event_details(e)
                                if calc:
                                    # 只显示未来的(days>=0)，或者纪念日(total>0)
                                    if calc['days'] >= 0 or calc['total'] > 0:
                                        candidate_events.append({
                                            'id': e['id'],
                                            'title': e['title'],
                                            'data': calc,
                                            'type': 'event',
                                            'is_lunar': e['event_type'] == 'lunar'
                                        })
                        except:
                            pass

                        # 3. 排序与选取
                        if candidate_events:
                            # 排序逻辑：
                            # 第一优先级: 是否过期 (x['data']['days'] < 0)。False(0) 排前，True(1) 排后
                            # 第二优先级: 剩余天数的绝对值 (越近越前)
                            candidate_events.sort(key=lambda x: (
                                1 if x['data']['days'] < 0 else 0,
                                abs(x['data']['days'])
                            ))

                            f['top_event'] = candidate_events[0]
                            f['all_events'] = candidate_events

                        # === 2. 天气缓存逻辑 (核心升级) ===
                        # 默认先读数据库里的旧缓存 (秒开的核心)
                        f['weather_home'] = f.get('weather_data_home')
                        f['weather_away'] = f.get('weather_data_away')

                        # 判断是否需要更新 (缓存策略: 30分钟)
                        need_update = False
                        last_update_str = f.get('last_weather_update')

                        if not last_update_str:
                            need_update = True  # 没存过，必须更新
                        else:
                            try:
                                # 解析数据库时间 (处理 ISO 格式)
                                last_time = datetime.fromisoformat(last_update_str.replace('Z', '+00:00'))
                                # 如果过去超过 30 分钟 -> 更新
                                if (utc_now - last_time) > timedelta(minutes=30):
                                    need_update = True
                            except:
                                need_update = True  # 时间格式错了，重来

                        # === 3. 执行更新 (只有过期了才跑这一步) ===
                        if need_update:
                            print(f"🔄 缓存过期，正在更新家庭 [{f['name']}] 的天气...")
                            new_home = None
                            new_away = None

                            # 查老家
                            if f.get('location_home_id'):
                                new_home = get_weather_full(
                                    f['location_home_id'],
                                    f.get('location_home_lat'),
                                    f.get('location_home_lon')
                                )
                                if new_home: f['weather_home'] = new_home  # 实时覆盖内存数据

                            # 查远方
                            if f.get('location_away_id'):
                                new_away = get_weather_full(
                                    f['location_away_id'],
                                    f.get('location_away_lat'),
                                    f.get('location_away_lon')
                                )
                                if new_away: f['weather_away'] = new_away  # 实时覆盖内存数据

                            # 写回数据库 (只在有新数据时写入)
                            if new_home or new_away:
                                try:
                                    update_payload = {'last_weather_update': utc_now.isoformat()}
                                    if new_home: update_payload['weather_data_home'] = new_home
                                    if new_away: update_payload['weather_data_away'] = new_away

                                    # 异步写入数据库
                                    db.table('families').update(update_payload).eq('id', f['id']).execute()
                                except Exception as e:
                                    print(f"Cache Write Error: {e}")

                        # [新增] 获取许愿菜单 (按状态排序: 想吃 -> 已买 -> 吃过)
                        f['wishes'] = []
                        try:
                            wishes_res = db.table('family_wishes') \
                                .select('*') \
                                .eq('family_id', f['id']) \
                                .order('created_at', desc=True) \
                                .execute()

                            # 简单的本地排序优化：把"吃到了"沉底
                            raw_wishes = wishes_res.data or []
                            # 排序优先级: wanted(0) > bought(1) > eaten(2)
                            status_order = {'wanted': 0, 'bought': 1, 'eaten': 2}
                            f['wishes'] = sorted(raw_wishes, key=lambda x: status_order.get(x['status'], 0))
                        except:
                            pass
                        f['reminders'] = []
                        try:
                            # 1. 计算24小时前的时间
                            yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

                            # 2. 查询数据
                            rem_res = db.table('family_reminders') \
                                .select('*') \
                                .eq('family_id', f['id']) \
                                .gte('created_at', yesterday) \
                                .order('created_at', desc=True) \
                                .limit(3) \
                                .execute()

                            reminders = rem_res.data or []

                            # 3. [关键修复] 遍历处理时间：UTC -> 北京时间
                            for r in reminders:
                                try:
                                    # 解析 UTC 时间字符串
                                    dt_utc = datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
                                    # 转为北京时间
                                    dt_bj = dt_utc.astimezone(timezone(timedelta(hours=8)))
                                    # 格式化为 "18:30" 这种格式
                                    r['time_display'] = dt_bj.strftime('%H:%M')
                                except:
                                    r['time_display'] = "刚刚"

                            f['reminders'] = reminders
                        except Exception as e:
                            print(f"Reminder Error: {e}")

    except Exception as e:
        print(f"Profile/Weather Fetch Error: {e}")

    if my_profile.get('display_name'): session['display_name'] = my_profile['display_name']
    user_name = session.get('display_name', '家人')

    # ================= 2. 获取可见成员映射 =================
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
                # [修改] 多查一个 status 字段
                profiles_res = db.table('profiles').select("id, display_name, avatar_url, status").in_('id',
                                                                                                       visible_user_ids).execute()
                for p in profiles_res.data:
                    avatar_link = None
                    if p.get('avatar_url'):
                        avatar_link = f"{url}/storage/v1/object/public/family_photos/{p['avatar_url']}"

                    # [修改] 把 status 也存进去
                    user_map[p['id']] = {
                        'name': p['display_name'],
                        'avatar': avatar_link,
                        'status': p.get('status', 'online')  # 默认在线
                    }
        else:
            p = my_profile
            user_map[p.get('id')] = {'name': p.get('display_name'), 'avatar': p.get('full_avatar_url')}
    except:
        pass

    # ================= 3. 获取核心数据 =================
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
                all_owners_res = db.table('pet_owners').select('pet_id, user_id').in_('pet_id', all_pet_ids).execute()
                for item in all_owners_res.data:
                    pid = item['pet_id']
                    uid = item['user_id']
                    if pid not in pet_owners_map: pet_owners_map[pid] = []
                    pet_owners_map[pid].append(uid)

            # 日志
            if all_pet_ids:
                logs = db.table('logs').select("*").in_('pet_id', all_pet_ids).gte('created_at', today_str).order(
                    'created_at', desc=True).execute().data or []

            # 动态
            visible_uids = list(user_map.keys())
            if visible_uids:
                moments_data = db.table('moments').select("*").in_('user_id', visible_uids).order('created_at',
                                                                                                  desc=True).limit(
                    20).execute().data or []
    except Exception as e:
        print(f"Data Fetch Error: {e}")

    # ================= 4. 数据组装 =================
    for pet in pets:
        pet['today_feed'] = False;
        pet['today_walk'] = False
        pet['feed_info'] = "";
        pet['walk_info'] = ""
        pet['latest_photo'] = None;
        pet['photo_uploader'] = ""
        pet['latest_log_id'] = None;
        pet['latest_user_id'] = None

        pet['owner_ids'] = pet_owners_map.get(pet['id'], [])
        pet['is_owner'] = (current_user_id in pet['owner_ids']) or session.get('is_impersonator')

        fam_obj = next((f for f in my_families if f['id'] == pet['family_id']), None)
        pet['family_name'] = fam_obj['name'] if fam_obj else ""

        for log in logs:
            if log['pet_id'] == pet['id']:
                who = user_map.get(log['user_id'], {}).get('name', '家人')
                time_str = format_time_friendly(log['created_at'])
                if log['action'] == 'feed':
                    pet['today_feed'] = True
                    if not pet['feed_info']: pet['feed_info'] = f"{who} ({time_str})"
                elif log['action'] == 'walk':
                    pet['today_walk'] = True
                    if not pet['walk_info']: pet['walk_info'] = f"{who} ({time_str})"
                elif log['action'] == 'photo' and not pet['latest_photo']:
                    if log.get('image_path'):
                        pet['latest_photo'] = f"{url}/storage/v1/object/public/family_photos/{log['image_path']}"
                        pet['photo_uploader'] = who
                        pet['latest_log_id'] = log['id']
                        pet['latest_user_id'] = log['user_id']

    moments = []
    for m in moments_data:
        u_info = user_map.get(m['user_id'], {})
        m['user_name'] = u_info.get('name', '家人')
        m['user_avatar'] = u_info.get('avatar')
        m['time_str'] = format_time_friendly(m['created_at'])
        if m.get('image_path'):
            m['image_url'] = f"{url}/storage/v1/object/public/family_photos/{m['image_path']}"
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

            # 补全图片URL
            for p in photos:
                if p.get('image_path'):
                    p['url'] = f"{url}/storage/v1/object/public/family_photos/{p['image_path']}"

            # 智能决定封面：有设定用设定，没设定用最新照片
            if cover_path:
                pet['cover_url'] = f"{url}/storage/v1/object/public/family_photos/{cover_path}"
            elif photos:
                pet['cover_url'] = photos[0]['url']
            else:
                # 默认封面 (可以是网图或者本地图)
                pet['cover_url'] = "/static/default_cover.jpg"  # 暂时用个占位，或者前端CSS处理

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

    return render_template('pet_detail.html', pet=pet)


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
        # [新增] 频率限制逻辑
        # 1. 查该家庭最新的一条提醒
        last_rem = db.table('family_reminders') \
            .select('created_at') \
            .eq('family_id', family_id) \
            .order('created_at', desc=True) \
            .limit(1) \
            .execute()

        if last_rem.data:
            # 2. 转换时间并比对日期
            last_time = datetime.fromisoformat(last_rem.data[0]['created_at'].replace('Z', '+00:00'))
            last_date = last_time.astimezone(timezone(timedelta(hours=8))).date()
            today_date = datetime.now(timezone(timedelta(hours=8))).date()

            # 3. 如果今天是同一天，拦截
            if last_date == today_date:
                flash("今天已经提醒过啦，不用太唠叨哦~ (每天限1条)", "info")
                return redirect(url_for('home'))

        # ... (后续的插入逻辑保持不变) ...
        sender_name = session.get('display_name', '家人')
        db.table('family_reminders').insert({
            'family_id': family_id,
            'content': content,
            'sender_name': sender_name
        }).execute()

        flash("提醒已发送", "success")
    except Exception as e:
        flash(f"发送失败: {e}", "danger")

    return redirect(url_for('home'))


@app.route('/create_family', methods=['POST'])
@login_required
def create_family():
    # ⚠️ 关键修改：使用 admin_supabase (上帝权限) 来创建
    # 这样可以绕过 "必须先是成员才能看到家庭ID" 的死锁问题
    if admin_supabase:
        client = admin_supabase
    else:
        # 如果没配置 Service Key，只能回退到普通用户（依然会报错，所以必须配 Service Key）
        client = get_db()
        print("⚠️ 警告: 缺少 Service Key，创建家庭可能会失败")

    family_name = request.form.get('family_name')

    if not family_name:
        flash("家庭名称不能为空", "warning")
        return redirect(url_for('home', tab='mine'))

    try:
        code = generate_invite_code()

        # 1. 使用上帝权限插入家庭，这样能拿到 ID
        res = client.table('families').insert({
            "name": family_name,
            "invite_code": code
        }).execute()

        if res.data and len(res.data) > 0:
            new_fam_id = res.data[0]['id']

            # 2. 依然使用上帝权限，把自己绑定进这个家庭
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
    if not admin_supabase:
        flash("缺少 Service Key，无法查询邀请码", "danger")
        return redirect(url_for('home', tab='mine'))

    code = request.form.get('invite_code')
    if not code: return redirect(url_for('home', tab='mine'))

    try:
        # 1. 查家庭
        fam = admin_supabase.table('families').select('id, name').eq('invite_code', code.upper()).single().execute()
        if fam.data:
            target_id = fam.data['id']
            # 2. [修改] 插入中间表 (如果已存在会报错，我们在 SQL 设置了 unique)
            try:
                get_db().table('family_members').insert({
                    'family_id': target_id,
                    'user_id': session['user']
                }).execute()
                flash(f"成功加入 [{fam.data['name']}]", "success")
            except Exception as e:
                if "duplicate" in str(e):
                    flash("你已经在该家庭里了", "warning")
                else:
                    raise e
        else:
            flash("邀请码无效", "warning")
    except Exception as e:
        flash(f"加入失败: {e}", "danger")
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
    """更新个人资料 (含关怀模式)"""
    db = get_db()
    display_name = request.form.get('display_name')
    file = request.files.get('avatar')
    # [新增] 获取开关状态 (checkbox 选中发 'on'，没选中发 None)
    is_elder = request.form.get('is_elder_mode') == 'on'

    update_data = {'is_elder_mode': is_elder}

    if display_name:
        update_data['display_name'] = display_name

    if file and file.filename:
        try:
            filename = secure_filename(file.filename)
            file_path = f"avatar_{session['user']}_{int(datetime.now().timestamp())}_{filename}"
            db.storage.from_("family_photos").upload(file_path, file.read(), {"content-type": file.content_type})
            update_data['avatar_url'] = file_path
        except Exception as e:
            flash(f"头像上传失败: {e}", "danger")

    try:
        db.table('profiles').update(update_data).eq('id', session['user']).execute()
        flash("个人资料已更新", "success")
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
    except Exception as e:
        print(f"Admin Data Error: {e}")
        users = [];
        pets = [];
        families = [];
        members = [];
        pet_owners_data = [];
        updates_list = [];
        reg_codes = []

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
    if admin_supabase:
        try:
            file_owner = {}
            # 扫描 Logs (宠物照片)
            logs = client.table('logs').select('image_path, user_id').neq('image_path', 'null').execute().data
            for l in logs: file_owner[l['image_path']] = user_name_map.get(l['user_id'], '未知')

            # 扫描 Moments (动态照片)
            moms = client.table('moments').select('image_path, user_id').neq('image_path', 'null').execute().data
            for m in moms: file_owner[m['image_path']] = user_name_map.get(m['user_id'], '未知')

            # 扫描 Profiles (头像)
            for u in users:
                if u.get('avatar_url'): file_owner[u['avatar_url']] = u['display_name'] + " (头像)"

            # 遍历文件列表
            files = client.storage.from_("family_photos").list()
            for f in files:
                name = f['name']
                if name == '.emptyFolderPlaceholder': continue

                size = f.get('metadata', {}).get('size', 0)
                total_size += size

                uploader = file_owner.get(name)
                uploader_str = f"✅ {uploader}" if uploader else '⚠️ 无记录'

                storage_files.append({
                    "name": name,
                    "size_kb": round(size / 1024, 2),
                    "created_at_fmt": f.get('created_at', '')[:19].replace('T', ' '),
                    "url": client.storage.from_("family_photos").get_public_url(name),
                    "uploader": uploader_str
                })
            storage_files.sort(key=lambda x: x['created_at_fmt'], reverse=True)
        except Exception as e:
            print(f"Storage Error: {e}")

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
        "file_count": len(storage_files)
    }

    return render_template('admin.html',
                           users=users,  # 用户列表
                           pets=pets,  # 宠物列表 (含主人信息)
                           families=families,  # 家庭列表 (含人数)
                           files=storage_files,  # 文件列表 (含上传者)
                           stats=stats,  # 顶部统计数字
                           auth_users=auth_users,  # 底层 Auth 用户
                           updates=updates_list,  # 更新日志列表
                           reg_codes=reg_codes,  # [新增] 注册暗号列表
                           user_name=session.get('display_name'))


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
    action = request.form.get('action')  # 'next_status' 或 'delete'
    current_status = request.form.get('current_status')

    try:
        if action == 'delete':
            db.table('family_wishes').delete().eq('id', wish_id).execute()
            flash("已删除该菜品", "info")

        elif action == 'next_status':
            # 状态流转: wanted -> bought -> eaten -> wanted (循环)
            new_status = 'bought'
            if current_status == 'bought':
                new_status = 'eaten'
            elif current_status == 'eaten':
                new_status = 'wanted'

            db.table('family_wishes').update({'status': new_status}).eq('id', wish_id).execute()
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
    """拍一拍家人"""
    db = get_db()
    target_uid = request.form.get('target_uid')
    target_name = request.form.get('target_name')
    family_id = request.form.get('family_id')

    if not target_uid or not family_id: return redirect(url_for('home'))

    try:
        my_name = session.get('display_name', '我')
        # 构造拍一拍文案
        msg = f"👋 {my_name} 拍了拍 {target_name}"

        # 写入家庭提醒表 (复用现有的提醒功能)
        db.table('family_reminders').insert({
            'family_id': family_id,
            'content': msg,
            'sender_name': '系统'
        }).execute()

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


# ================= 🎄 圣诞彩蛋数据接口 =================

@app.route('/api/christmas_data')
@login_required
def christmas_data():
    """获取圣诞树所需的素材：照片 + 愿望"""
    db = get_db()
    current_user_id = session.get('user')

    data_payload = {
        'photos': [],  # 挂在树上的照片
        'wishes': []  # 树下的礼物盒
    }

    try:
        # 1. 获取我的家庭列表
        members_res = db.table('family_members').select('family_id').eq('user_id', current_user_id).execute()
        my_fam_ids = [m['family_id'] for m in members_res.data] if members_res.data else []

        if not my_fam_ids:
            return jsonify(data_payload)  # 没家庭就返回空

        # -------------------------------------------
        # 2. 获取照片素材 (限制 50 张，防止手机卡死)
        # -------------------------------------------

        # A. 宠物照片 (Logs)
        # 先查出这些家庭里的宠物 ID
        pets_res = db.table('pets').select('id, name').in_('family_id', my_fam_ids).execute()
        pet_ids = [p['id'] for p in pets_res.data] if pets_res.data else []

        if pet_ids:
            logs_res = db.table('logs').select('image_path, created_at, pets(name)') \
                .in_('pet_id', pet_ids) \
                .eq('action', 'photo') \
                .order('created_at', desc=True) \
                .limit(30) \
                .execute()

            for log in (logs_res.data or []):
                if log.get('image_path'):
                    data_payload['photos'].append({
                        'type': 'pet',
                        'url': f"{url}/storage/v1/object/public/family_photos/{log['image_path']}",
                        'text': f"{log['pets']['name']} 的照片",
                        'date': log['created_at'][:10]
                    })

        # B. 动态照片 (Moments)
        # 查找家庭内成员发的动态
        co_members = db.table('family_members').select('user_id').in_('family_id', my_fam_ids).execute()
        member_ids = list(set([m['user_id'] for m in co_members.data])) if co_members.data else []

        if member_ids:
            mom_res = db.table('moments').select('image_path, created_at, content') \
                .in_('user_id', member_ids) \
                .neq('image_path', 'null') \
                .order('created_at', desc=True) \
                .limit(20) \
                .execute()

            for m in (mom_res.data or []):
                data_payload['photos'].append({
                    'type': 'moment',
                    'url': f"{url}/storage/v1/object/public/family_photos/{m['image_path']}",
                    'text': m['content'] or "美好瞬间",
                    'date': m['created_at'][:10]
                })

        # -------------------------------------------
        # 3. 获取愿望 (作为树下的礼物盒)
        # -------------------------------------------
        wish_res = db.table('family_wishes').select('*').in_('family_id', my_fam_ids).execute()
        for w in (wish_res.data or []):
            # 区分状态颜色：wanted(红), bought(金), eaten(绿)
            color = "#ff6b6b"  # 默认红
            if w['status'] == 'bought':
                color = "#feca57"  # 金
            elif w['status'] == 'eaten':
                color = "#1dd1a1"  # 绿

            data_payload['wishes'].append({
                'content': w['content'],
                'status': w['status'],
                'color': color
            })

    except Exception as e:
        print(f"Christmas Data Error: {e}")

    # 打乱照片顺序，让宠物和生活照混在一起更自然
    random.shuffle(data_payload['photos'])

    return jsonify(data_payload)

@app.route('/christmas')
@login_required
def christmas_page():
    """渲染圣诞树页面"""
    return render_template('christmas.html')
if __name__ == '__main__':
    # 开发环境启动
    app.run(debug=True, host='0.0.0.0', port=5000)
