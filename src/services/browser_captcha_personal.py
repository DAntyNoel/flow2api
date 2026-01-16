import asyncio
import time
import re
import os
import random
from typing import Optional, Dict
from playwright.async_api import async_playwright, BrowserContext, Page

from ..core.logger import debug_logger

def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """解析代理URL，分离协议、主机、端口、认证信息"""
    proxy_pattern = r'^(socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$'
    match = re.match(proxy_pattern, proxy_url)
    if match:
        protocol, username, password, host, port = match.groups()
        proxy_config = {'server': f'{protocol}://{host}:{port}'}
        if username and password:
            proxy_config['username'] = username
            proxy_config['password'] = password
        return proxy_config
    return None

class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（持久化有头模式）"""

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.headless = False 
        self.playwright = None
        self.context: Optional[BrowserContext] = None 
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        self.user_data_dir = os.path.join(os.getcwd(), "browser_data")
        
        # === 新增: 后台刷新相关配置 ===
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_running = False
        self.refresh_config = {
            'enabled': True,  # 是否启用后台刷新
            'min_interval': 300,  # 最小间隔(秒) - 5分钟
            'max_interval': 7200,  # 最大间隔(秒) - 2小时
            'visit_duration': (10, 30),  # 每次访问停留时间范围(秒)
            'scroll_probability': 0.7,  # 滚动页面的概率
            'mouse_move_probability': 0.5,  # 移动鼠标的概率
        }

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance

    async def initialize(self):
        """初始化持久化浏览器上下文"""
        if self._initialized and self.context:
            return

        try:
            proxy_url = None
            if self.db:
                captcha_config = await self.db.get_captcha_config()
                if captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
                    proxy_url = captcha_config.browser_proxy_url
                
                # 从数据库加载refresh配置
                self.refresh_config['enabled'] = captcha_config.refresh_enabled
                self.refresh_config['min_interval'] = captcha_config.refresh_min_interval
                self.refresh_config['max_interval'] = captcha_config.refresh_max_interval
                self.refresh_config['visit_duration'] = (
                    captcha_config.refresh_visit_duration_min,
                    captcha_config.refresh_visit_duration_max
                )
                self.refresh_config['scroll_probability'] = captcha_config.refresh_scroll_probability
                self.refresh_config['mouse_move_probability'] = captcha_config.refresh_mouse_move_probability
                
                debug_logger.log_info(f"[BrowserCaptcha] 已加载refresh配置: enabled={self.refresh_config['enabled']}, "
                                    f"interval={self.refresh_config['min_interval']}-{self.refresh_config['max_interval']}s")

            debug_logger.log_info(f"[BrowserCaptcha] 正在启动浏览器 (用户数据目录: {self.user_data_dir})...")
            self.playwright = await async_playwright().start()

            launch_options = {
                'headless': self.headless,
                'user_data_dir': self.user_data_dir,
                'viewport': {'width': 1280, 'height': 720},
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                ]
            }

            if proxy_url:
                proxy_config = parse_proxy_url(proxy_url)
                if proxy_config:
                    launch_options['proxy'] = proxy_config
                    debug_logger.log_info(f"[BrowserCaptcha] 使用代理: {proxy_config['server']}")

            self.context = await self.playwright.chromium.launch_persistent_context(**launch_options)
            self.context.set_default_timeout(30000)

            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 浏览器已启动 (Profile: {self.user_data_dir})")
            
            # === 新增: 启动后台刷新任务 ===
            if self.refresh_config['enabled']:
                await self.start_background_refresh()
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 浏览器启动失败: {str(e)}")
            raise

    async def get_token(self, project_id: str) -> Optional[str]:
        """获取 reCAPTCHA token"""
        was_refreshing = self._refresh_running
        if was_refreshing:
            await self.stop_background_refresh()
        
        try:
            if not self._initialized or not self.context:
                await self.initialize()

            page: Optional[Page] = None

            try:
                page = await self.context.new_page()

                website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
                debug_logger.log_info(f"[BrowserCaptcha] 访问页面: {website_url}")

                try:
                    await page.goto(website_url, wait_until="domcontentloaded")
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 页面加载警告: {str(e)}")

                script_loaded = await page.evaluate("() => { return !!(window.grecaptcha && window.grecaptcha.execute); }")
                if not script_loaded:
                    await page.evaluate(f"""
                        () => {{
                            const script = document.createElement('script');
                            script.src = 'https://www.google.com/recaptcha/api.js?render={self.website_key}';
                            script.async = true; script.defer = true;
                            document.head.appendChild(script);
                        }}
                    """)
                    await page.wait_for_timeout(5000) 

                token = await page.evaluate(f"""
                    async () => {{
                        try {{
                            return await window.grecaptcha.execute('{self.website_key}', {{ action: 'FLOW_GENERATION' }});
                        }} catch (e) {{ return null; }}
                    }}
                """)
                
                if token:
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ Token获取成功")
                    return token
                else:
                    debug_logger.log_error("[BrowserCaptcha] Token获取失败")
                    return None

            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 异常: {str(e)}")
                return None
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception as e:
                        # 忽略页面关闭时的常见错误，但记录意外情况以便排查
                        msg = str(e)
                        if "Target page, context or browser has been closed" not in msg and "Connection closed" not in msg:
                            debug_logger.log_warning(f"[BrowserCaptcha] 页面关闭警告: {msg}")
        finally:
            if was_refreshing and self._initialized:
                await self.start_background_refresh()

    async def close(self):
        """完全关闭浏览器（清理资源时调用）"""
        try:
            await self.stop_background_refresh()
            
            if self.context:
                try:
                    await self.context.close()
                except Exception as e:
                    # 忽略关闭时的连接错误（程序退出时正常现象）
                    if "Connection closed" not in str(e) and "Target page, context or browser has been closed" not in str(e):
                        debug_logger.log_warning(f"[BrowserCaptcha] Context关闭警告: {str(e)}")
                finally:
                    self.context = None
            
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception as e:
                    # 忽略 playwright 停止时的错误
                    if "Connection closed" not in str(e):
                        debug_logger.log_warning(f"[BrowserCaptcha] Playwright停止警告: {str(e)}")
                finally:
                    self.playwright = None
                
            self._initialized = False
            debug_logger.log_info("[BrowserCaptcha] 浏览器服务已关闭")
        except Exception as e:
            # 只记录意外的错误
            debug_logger.log_warning(f"[BrowserCaptcha] 关闭时发生异常: {str(e)}")

    async def start_background_refresh(self):
        """启动后台刷新任务"""
        if self._refresh_running:
            debug_logger.log_warning("[BrowserRefresh] 后台刷新已在运行中")
            return
            
        self._refresh_running = True
        self._refresh_task = asyncio.create_task(self._background_refresh_loop())
        debug_logger.log_info("[BrowserRefresh] 🔄 后台刷新任务已启动")

    async def stop_background_refresh(self):
        """停止后台刷新任务"""
        if not self._refresh_running:
            return
            
        self._refresh_running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        debug_logger.log_info("[BrowserRefresh] ⏸️ 后台刷新任务已停止")

    async def _background_refresh_loop(self):
        """后台刷新循环"""
        while self._refresh_running:
            try:
                interval = random.uniform(
                    self.refresh_config['min_interval'],
                    self.refresh_config['max_interval']
                )
                debug_logger.log_info(f"[BrowserRefresh] 下次刷新将在 {interval/60:.1f} 分钟后")
                await asyncio.sleep(interval)
                
                if not self._refresh_running:
                    break
                    
                await self._simulate_human_visit()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_error(f"[BrowserRefresh] 刷新循环异常: {str(e)}")
                await asyncio.sleep(60)

    async def _simulate_human_visit(self):
        """模拟人类访问行为"""
        page: Optional[Page] = None
        try:
            if not self.context:
                debug_logger.log_warning("[BrowserRefresh] 浏览器未初始化,跳过刷新")
                return
                
            page = await self.context.new_page()
            
            target_urls = [
                "https://www.google.com",
                "https://labs.google/fx/tools/flow",
                "https://www.google.com/search?q=google+gemini",
                "https://www.google.com/search?q=deepseek",
                "https://www.google.com/search?q=claude+ai",
                "https://www.google.com/search?q=openai+gpt-5",
                "https://www.google.com/search?q=openai+chatgpt",
                "https://www.wikipedia.org",
            ]
            target_url = random.choice(target_urls)
            
            debug_logger.log_info(f"[BrowserRefresh] 🌐 模拟访问: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded")
            
            # 在主页面停留并执行行为
            main_visit_duration = random.uniform(*self.refresh_config['visit_duration']) * 0.6  # 60%的时间在主页面
            await self._simulate_human_behavior(page, main_visit_duration)
            
            # 提取页面中的所有链接并随机访问1-3个
            await self._visit_random_links(page)
            
            debug_logger.log_info(f"[BrowserRefresh] ✅ 访问完成")
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserRefresh] 模拟访问异常: {str(e)}")
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    async def _visit_random_links(self, page: Page):
        """提取页面链接并随机访问1-3个"""
        try:
            # 提取页面中所有的链接
            links = await page.evaluate("""
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    return anchors
                        .map(a => a.href)
                        .filter(href => {
                            // 过滤掉无效链接和特殊协议
                            return href && 
                                   href.startsWith('http') && 
                                   !href.includes('javascript:') &&
                                   !href.includes('mailto:') &&
                                   !href.includes('#') &&
                                   href.length < 200;  // 避免过长的URL
                        })
                        .slice(0, 50);  // 最多取前50个
                }
            """)
            
            if not links or len(links) == 0:
                debug_logger.log_info("[BrowserRefresh] 未找到可访问的链接")
                return
            
            # 随机选择访问1-3个链接
            num_links = random.randint(1, min(3, len(links)))
            selected_links = random.sample(links, num_links)
            
            debug_logger.log_info(f"[BrowserRefresh] 📎 从 {len(links)} 个链接中选择访问 {num_links} 个")
            
            for i, link in enumerate(selected_links, 1):
                try:
                    # 显示简短的URL用于日志
                    short_url = link if len(link) < 60 else link[:57] + "..."
                    debug_logger.log_info(f"[BrowserRefresh] 🔗 访问链接 {i}/{num_links}: {short_url}")
                    
                    # 访问链接
                    await page.goto(link, wait_until="domcontentloaded", timeout=15000)
                    
                    # 在每个链接页面停留一小段时间并执行简单行为
                    link_duration = random.uniform(3, 8)
                    await self._simulate_human_behavior(page, link_duration)
                    
                    # 链接之间的间隔
                    if i < num_links:
                        await asyncio.sleep(random.uniform(1, 3))
                        
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserRefresh] 访问链接失败: {str(e)[:100]}")
                    # 继续访问下一个链接
                    continue
                    
        except Exception as e:
            debug_logger.log_warning(f"[BrowserRefresh] 提取链接异常: {str(e)}")

    async def _simulate_human_behavior(self, page: Page, duration: float):
        """在页面上模拟人类行为"""
        start_time = time.time()
        actions_performed = []
        
        while (time.time() - start_time) < duration:
            remaining_time = duration - (time.time() - start_time)
            if remaining_time <= 0:
                break
                
            action = random.choice([
                'scroll',
                'mouse_move', 
                'click_element',
                'scroll',
                'mouse_move', 
                'click_element',
                'scroll',
                'mouse_move', 
                'click_element',
                'wait',
            ])
            
            try:
                if action == 'scroll' and random.random() < self.refresh_config['scroll_probability']:
                    scroll_amount = random.randint(100, 500)
                    direction = random.choice(['down', 'up'])
                    
                    if direction == 'down':
                        await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                    else:
                        await page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
                    
                    actions_performed.append(f'scroll_{direction}')
                    await asyncio.sleep(random.uniform(0.5, 2))
                    
                elif action == 'mouse_move' and random.random() < self.refresh_config['mouse_move_probability']:
                    x = random.randint(100, 800)
                    y = random.randint(100, 600)
                    await page.mouse.move(x, y)
                    
                    actions_performed.append('mouse_move')
                    await asyncio.sleep(random.uniform(0.3, 1))
                    
                elif action == 'wait':
                    wait_time = min(random.uniform(2, 5), remaining_time)
                    await asyncio.sleep(wait_time)
                    actions_performed.append('wait')
                    
                elif action == 'click_element':
                    try:
                        search_box = await page.query_selector('input[type="text"], input[type="search"]')
                        if search_box:
                            await search_box.click()
                            actions_performed.append('click_search')
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                    except:
                        pass
                        
            except Exception as e:
                debug_logger.log_warning(f"[BrowserRefresh] 行为模拟小错误: {str(e)}")
                await asyncio.sleep(1)
        
        debug_logger.log_info(f"[BrowserRefresh] 执行的行为: {', '.join(actions_performed)}")

    def set_refresh_config(self, **kwargs):
        """
        动态配置后台刷新参数
        
        参数:
            enabled: bool - 是否启用
            min_interval: int - 最小间隔(秒)
            max_interval: int - 最大间隔(秒)
            visit_duration: tuple - 访问停留时间范围
            scroll_probability: float - 滚动概率 (0-1)
            mouse_move_probability: float - 鼠标移动概率 (0-1)
        """
        for key, value in kwargs.items():
            if key in self.refresh_config:
                self.refresh_config[key] = value
                debug_logger.log_info(f"[BrowserRefresh] 配置已更新: {key}={value}")

    async def reload_config(self):
        """
        从数据库重新加载配置并重启服务
        用于管理员修改配置后热重载
        """
        if not self.db:
            debug_logger.log_warning("[BrowserCaptcha] 无法重载配置: 数据库未初始化")
            return
        
        try:
            # 停止当前的后台刷新任务
            was_running = self._refresh_running
            if was_running:
                await self.stop_background_refresh()
                debug_logger.log_info("[BrowserCaptcha] 已停止后台刷新任务以重载配置")
            
            # 从数据库重新加载配置
            captcha_config = await self.db.get_captcha_config()
            self.refresh_config['enabled'] = captcha_config.refresh_enabled
            self.refresh_config['min_interval'] = captcha_config.refresh_min_interval
            self.refresh_config['max_interval'] = captcha_config.refresh_max_interval
            self.refresh_config['visit_duration'] = (
                captcha_config.refresh_visit_duration_min,
                captcha_config.refresh_visit_duration_max
            )
            self.refresh_config['scroll_probability'] = captcha_config.refresh_scroll_probability
            self.refresh_config['mouse_move_probability'] = captcha_config.refresh_mouse_move_probability
            
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 配置已重载: enabled={self.refresh_config['enabled']}, "
                                f"interval={self.refresh_config['min_interval']}-{self.refresh_config['max_interval']}s, "
                                f"scroll_prob={self.refresh_config['scroll_probability']}, "
                                f"mouse_prob={self.refresh_config['mouse_move_probability']}")
            
            # 如果配置启用了后台刷新，重新启动
            if self.refresh_config['enabled'] and self._initialized:
                await self.start_background_refresh()
                debug_logger.log_info("[BrowserCaptcha] ✅ 后台刷新任务已使用新配置重启")
            elif not self.refresh_config['enabled'] and was_running:
                debug_logger.log_info("[BrowserCaptcha] ℹ️ 后台刷新已被禁用，任务不会重启")
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 配置重载失败: {str(e)}")
            # 如果之前在运行，尝试恢复
            if was_running and self._initialized:
                try:
                    await self.start_background_refresh()
                except:
                    pass

    async def get_flow_cookies(self) -> Optional[Dict]:
        """
        访问 Google Flow 界面并获取 cookies
        
        返回:
            dict: 包含所有 cookies 的字典，格式为 {name: value, ...}
            None: 获取失败时返回
        """
        page: Optional[Page] = None
        try:
            if not self._initialized or not self.context:
                await self.initialize()
            
            page = await self.context.new_page()
            flow_url = "https://labs.google/fx/tools/flow"
            
            debug_logger.log_info(f"[BrowserCaptcha] 正在访问 Google Flow: {flow_url}")
            
            # 访问页面并等待加载完成
            await page.goto(flow_url, wait_until="domcontentloaded")
            
            # 等待页面稳定
            await page.wait_for_timeout(2000)
            
            # 获取所有 cookies
            cookies = await self.context.cookies()
            debug_logger.log_info(f"[BrowserCaptcha] 获取到 {len(cookies)} 个 cookies")
            debug_logger.log_info(f"[BrowserCaptcha] {cookies}")
            
            # 转换为更易用的字典格式
            cookie_dict = {cookie['name']: cookie['value'] for cookie in cookies}
            
            # 同时返回完整的 cookie 信息（包含 domain, path 等）
            result = {
                'simple': cookie_dict,  # 简单格式: {name: value}
                'detailed': cookies     # 详细格式: 包含所有属性的列表
            }
            
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 成功获取 {len(cookies)} 个 cookies")
            
            return result
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 获取 cookies 失败: {str(e)}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    async def open_login_window(self):
        """调用此方法打开一个永久窗口供你登录Google Flow"""
        await self.initialize()
        page = await self.context.new_page()
        await page.goto("https://labs.google/fx/tools/flow", wait_until="domcontentloaded")
    