import sys
import time
import uuid
import logging
import asyncio
import inspect
from typing import Optional, Union
import argparse
from quart import Quart, request, jsonify
try:
    from camoufox.async_api import AsyncCamoufox
except Exception:  # pragma: no cover
    AsyncCamoufox = None  # type: ignore

try:
    from patchright.async_api import async_playwright
except Exception:  # pragma: no cover
    from playwright.async_api import async_playwright
from db_results import init_db, save_result, load_result, cleanup_old_results
from browser_configs import browser_config
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box



COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("TurnstileAPIServer")  # type: ignore
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:

    def __init__(self, headless: bool, useragent: Optional[str], debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool = False, browser_name: Optional[str] = None, browser_version: Optional[str] = None, proxy_url: str = ""):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.proxy_url = proxy_url
        self.browser_pool = asyncio.Queue()
        self._playwright = None
        self._camoufox = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutting_down = False
        self._browser_instances: dict[int, object] = {}
        self._browser_configs: dict[int, dict] = {}
        self.use_random_config = use_random_config
        self.browser_name = browser_name
        self.browser_version = browser_version
        self.console = Console()
        
        # Initialize useragent and sec_ch_ua attributes
        self.useragent = useragent
        self.sec_ch_ua = None
        
        
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if browser_name and browser_version:
                config = browser_config.get_browser_config(browser_name, browser_version)
                if config:
                    useragent, sec_ch_ua = config
                    self.useragent = useragent
                    self.sec_ch_ua = sec_ch_ua
            elif useragent:
                self.useragent = useragent
            else:
                browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                self.browser_name = browser
                self.browser_version = version
                self.useragent = useragent
                self.sec_ch_ua = sec_ch_ua
        
        self.browser_args = []
        if self.useragent:
            self.browser_args.append(f"--user-agent={self.useragent}")

        self._setup_routes()

    def _get_browser_proxy(self) -> Optional[str]:
        """获取浏览器代理。自动启动场景只使用显式传入的 proxy_url。"""
        if self.proxy_url:
            return self.proxy_url
        if self.debug and self.proxy_support:
            logger.debug("Proxy support enabled without --proxy-url; running solver without proxy")
        return None

    @staticmethod
    def _parse_proxy(proxy: str) -> Optional[dict]:
        """解析代理字符串为 Playwright 代理配置。"""
        if not proxy:
            return None
        if '@' in proxy:
            try:
                scheme_part, auth_part = proxy.split('://')
                auth, address = auth_part.split('@')
                username, password = auth.split(':')
                ip, port = address.split(':')
                return {
                    "server": f"{scheme_part}://{ip}:{port}",
                    "username": username,
                    "password": password,
                }
            except ValueError:
                raise ValueError(f"Invalid proxy format: {proxy}")
        parts = proxy.split(':')
        if len(parts) == 5:
            p_scheme, p_ip, p_port, p_user, p_pass = parts
            return {
                "server": f"{p_scheme}://{p_ip}:{p_port}",
                "username": p_user,
                "password": p_pass,
            }
        elif len(parts) == 3:
            return {"server": proxy}
        # proxy_url 形式 (http://host:port)
        if proxy.startswith("http://") or proxy.startswith("https://") or proxy.startswith("socks"):
            return {"server": proxy}
        raise ValueError(f"Invalid proxy format: {proxy}")

    def display_welcome(self):
        """Displays welcome screen with logo."""
        self.console.clear()
        
        combined_text = Text()
        combined_text.append("\nChannel: ", style="bold white")
        combined_text.append("https://t.me/D3_vin", style="cyan")
        combined_text.append("\nChat: ", style="bold white")
        combined_text.append("https://t.me/D3vin_chat", style="cyan")
        combined_text.append("\nGitHub: ", style="bold white")
        combined_text.append("https://github.com/D3-vin", style="cyan")
        combined_text.append("\nVersion: ", style="bold white")
        combined_text.append("1.2a", style="green")
        combined_text.append("\n")

        info_panel = Panel(
            Align.left(combined_text),
            title="[bold blue]Turnstile Solver[/bold blue]",
            subtitle="[bold magenta]Dev by D3vin[/bold magenta]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 1),
            width=50
        )

        try:
            self.console.print(info_panel)
            self.console.print()
        except UnicodeEncodeError:
            # Fallback for Windows consoles with non-UTF8 encoding
            print("Turnstile Solver")
            print("Channel: https://t.me/D3_vin")
            print("Chat: https://t.me/D3vin_chat")
            print("GitHub: https://github.com/D3-vin")
            print("Version: 1.2a")




    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.after_serving(self._shutdown)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/grok_setup', methods=['POST'])(self.grok_setup)
        self.app.route('/cf_clearance', methods=['POST'])(self.cf_clearance_handler)
        self.app.route('/')(self.index)
        

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        self.display_welcome()
        self._shutting_down = False
        logger.info("Starting browser initialization")
        try:
            await init_db()
            await self._initialize_browser()
            
            # Запускаем периодическую очистку старых результатов
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _shutdown(self) -> None:
        """Shutdown hook: cancel tasks and close pooled browsers/drivers."""
        self._shutting_down = True
        logger.info("Turnstile solver shutting down")

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Cleanup task shutdown error: {e}")
            finally:
                self._cleanup_task = None

        while True:
            try:
                self.browser_pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            except Exception:
                break

        browsers = list(self._browser_instances.items())
        self._browser_instances.clear()
        self._browser_configs.clear()
        for index, browser in browsers:
            await self._close_browser(index, browser, reason="solver shutdown")

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"Failed to stop playwright runtime: {e}")
            finally:
                self._playwright = None

        if self._camoufox:
            try:
                stopper = getattr(self._camoufox, "stop", None) or getattr(self._camoufox, "close", None)
                if stopper:
                    result = stopper()
                    if inspect.isawaitable(result):
                        await result
            except Exception as e:
                logger.warning(f"Failed to stop camoufox runtime: {e}")
            finally:
                self._camoufox = None

    async def _close_browser(self, index: int, browser, reason: str = "") -> None:
        """Best-effort close for a browser instance."""
        self._browser_instances.pop(index, None)
        if self.debug:
            logger.debug(f"Browser {index}: closing browser ({reason or 'no reason'})")
        try:
            close_method = getattr(browser, "close", None)
            if close_method:
                result = close_method()
                if inspect.isawaitable(result):
                    await result
        except Exception as e:
            logger.warning(f"Browser {index}: error closing browser: {str(e)}")

    async def _launch_browser_instance(self, index: int, config: dict):
        """Launch a single browser instance using stored runtime."""
        browser_args = [
            "--window-position=0,0",
            "--force-device-scale-factor=1"
        ]
        if config.get('useragent'):
            browser_args.append(f"--user-agent={config['useragent']}")

        browser = None
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if not self._playwright:
                raise RuntimeError("Playwright runtime is not initialized")
            launch_kwargs = {
                "headless": self.headless,
                "args": browser_args,
            }
            if self.browser_type in ["chrome", "msedge"]:
                launch_kwargs["channel"] = self.browser_type
            browser = await self._playwright.chromium.launch(**launch_kwargs)
        elif self.browser_type == "camoufox":
            if not self._camoufox:
                raise RuntimeError("Camoufox runtime is not initialized")
            browser = await self._camoufox.start()

        if not browser:
            raise RuntimeError(f"Failed to launch browser instance {index}")

        self._browser_instances[index] = browser
        self._browser_configs[index] = config
        return browser

    async def _replace_browser(self, index: int, browser, browser_config: dict, reason: str = "") -> None:
        """Drop a broken browser and try to launch a replacement."""
        await self._close_browser(index, browser, reason=reason or "replace")
        if self._shutting_down:
            return
        try:
            replacement = await self._launch_browser_instance(index, browser_config)
            await self.browser_pool.put((index, replacement, browser_config))
            logger.info(f"Browser {index}: replaced after {reason or 'runtime issue'}")
        except Exception as e:
            logger.error(f"Browser {index}: failed to replace browser after {reason or 'runtime issue'}: {str(e)}")

    async def _return_browser(self, index: int, browser, browser_config: dict) -> None:
        """Return a healthy browser to the pool, otherwise replace it."""
        if self._shutting_down:
            await self._close_browser(index, browser, reason="solver shutdown return")
            return

        connected = True
        try:
            if hasattr(browser, 'is_connected'):
                connected = bool(browser.is_connected())
        except Exception as e:
            connected = False
            logger.warning(f"Browser {index}: failed to inspect browser state, replacing: {str(e)}")

        if connected:
            self._browser_instances[index] = browser
            self._browser_configs[index] = browser_config
            await self.browser_pool.put((index, browser, browser_config))
            if self.debug:
                logger.debug(f"Browser {index}: returned to pool")
            return

        logger.warning(f"Browser {index}: disconnected, replacing browser instance")
        await self._replace_browser(index, browser, browser_config, reason="disconnected")

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        self._playwright = None
        self._camoufox = None

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            self._playwright = await async_playwright().start()
        elif self.browser_type == "camoufox":
            if AsyncCamoufox is None:
                raise RuntimeError("camoufox is not installed. Please install camoufox or use --browser_type chromium.")
            self._camoufox = AsyncCamoufox(headless=self.headless)

        browser_configs = []
        for _ in range(self.thread_count):
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                if self.use_random_config:
                    browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                elif self.browser_name and self.browser_version:
                    config = browser_config.get_browser_config(self.browser_name, self.browser_version)
                    if config:
                        useragent, sec_ch_ua = config
                        browser = self.browser_name
                        version = self.browser_version
                    else:
                        browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                else:
                    browser = getattr(self, 'browser_name', 'custom')
                    version = getattr(self, 'browser_version', 'custom')
                    useragent = self.useragent
                    sec_ch_ua = getattr(self, 'sec_ch_ua', '')
            else:
                # Для camoufox и других браузеров используем значения по умолчанию
                browser = self.browser_type
                version = 'custom'
                useragent = self.useragent
                sec_ch_ua = getattr(self, 'sec_ch_ua', '')

            
            browser_configs.append({
                'browser_name': browser,
                'browser_version': version,
                'useragent': useragent,
                'sec_ch_ua': sec_ch_ua
            })

        for i in range(self.thread_count):
            config = browser_configs[i]
            browser = await self._launch_browser_instance(i + 1, config)
            await self.browser_pool.put((i + 1, browser, config))

            if self.debug:
                logger.info(f"Browser {i + 1} initialized successfully with {config['browser_name']} {config['browser_version']}")

        logger.info(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")
        
        if self.use_random_config:
            logger.info(f"Each browser in pool received random configuration")
        elif self.browser_name and self.browser_version:
            logger.info(f"All browsers using configuration: {self.browser_name} {self.browser_version}")
        else:
            logger.info("Using custom configuration")
            
        if self.debug:
            for i, config in enumerate(browser_configs):
                logger.debug(f"Browser {i+1} config: {config['browser_name']} {config['browser_version']}")
                logger.debug(f"Browser {i+1} User-Agent: {config['useragent']}")
                logger.debug(f"Browser {i+1} Sec-CH-UA: {config['sec_ch_ua']}")

    async def _periodic_cleanup(self):
        """Periodic cleanup of old results every hour"""
        while True:
            try:
                await asyncio.sleep(3600)
                deleted_count = await cleanup_old_results(days_old=7)
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old results")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")

    async def _antishadow_inject(self, page):
        await page.add_init_script("""
          (function() {
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
              const shadow = originalAttachShadow.call(this, init);
              if (init.mode === 'closed') {
                window.__lastClosedShadowRoot = shadow;
              }
              return shadow;
            };
          })();
        """)



    async def _optimized_route_handler(self, route):
        """Оптимизированный обработчик маршрутов для экономии ресурсов."""
        url = route.request.url
        resource_type = route.request.resource_type

        allowed_types = {'document', 'script', 'xhr', 'fetch'}

        allowed_domains = [
            'challenges.cloudflare.com',
            'static.cloudflareinsights.com',
            'cloudflare.com'
        ]
        
        if resource_type in allowed_types:
            await route.continue_()
        elif any(domain in url for domain in allowed_domains):
            await route.continue_() 
        else:
            await route.abort()

    async def _block_rendering(self, page):
        """Блокировка рендеринга для экономии ресурсов"""
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        """Разблокировка рендеринга"""
        await page.unroute("**/*", self._optimized_route_handler)

    async def _find_turnstile_elements(self, page, index: int):
        """Умная проверка всех возможных Turnstile элементов"""
        selectors = [
            '.cf-turnstile',
            '[data-sitekey]',
            'iframe[src*="turnstile"]',
            'iframe[title*="widget"]',
            'div[id*="turnstile"]',
            'div[class*="turnstile"]'
        ]
        
        elements = []
        for selector in selectors:
            try:
                # Безопасная проверка count()
                try:
                    count = await page.locator(selector).count()
                except Exception:
                    # Если count() дает ошибку, пропускаем этот селектор
                    continue
                    
                if count > 0:
                    elements.append((selector, count))
                    if self.debug:
                        logger.debug(f"Browser {index}: Found {count} elements with selector '{selector}'")
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Selector '{selector}' failed: {str(e)}")
                continue
        
        return elements

    async def _find_and_click_checkbox(self, page, index: int):
        """Найти и кликнуть по чекбоксу Turnstile CAPTCHA внутри iframe"""
        try:
            # Пробуем разные селекторы iframe с защитой от ошибок
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]'
            ]
            
            iframe_locator = None
            for selector in iframe_selectors:
                try:
                    test_locator = page.locator(selector).first
                    # Безопасная проверка count для iframe
                    try:
                        iframe_count = await test_locator.count()
                    except Exception:
                        iframe_count = 0
                        
                    if iframe_count > 0:
                        iframe_locator = test_locator
                        if self.debug:
                            logger.debug(f"Browser {index}: Found Turnstile iframe with selector: {selector}")
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Iframe selector '{selector}' failed: {str(e)}")
                    continue
            
            if iframe_locator:
                try:
                    # Получаем frame из iframe
                    iframe_element = await iframe_locator.element_handle()
                    frame = await iframe_element.content_frame()
                    
                    if frame:
                        # Ищем чекбокс внутри iframe
                        checkbox_selectors = [
                            'input[type="checkbox"]',
                            '.cb-lb input[type="checkbox"]',
                            'label input[type="checkbox"]'
                        ]
                        
                        for selector in checkbox_selectors:
                            try:
                                # Полностью избегаем locator.count() в iframe - используем альтернативный подход
                                try:
                                    # Пробуем кликнуть напрямую без count проверки
                                    checkbox = frame.locator(selector).first
                                    await checkbox.click(timeout=2000)
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Successfully clicked checkbox in iframe with selector '{selector}'")
                                    return True
                                except Exception as click_e:
                                    # Если прямой клик не сработал, записываем в debug но не падаем
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Direct checkbox click failed for '{selector}': {str(click_e)}")
                                    continue
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Iframe checkbox selector '{selector}' failed: {str(e)}")
                                continue
                    
                        # Если нашли iframe, но не смогли кликнуть чекбокс, пробуем клик по iframe
                        try:
                            if self.debug:
                                logger.debug(f"Browser {index}: Trying to click iframe directly as fallback")
                            await iframe_locator.click(timeout=1000)
                            return True
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Iframe direct click failed: {str(e)}")
                
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Failed to access iframe content: {str(e)}")
            
        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: General iframe search failed: {str(e)}")
        
        return False

    async def _try_click_strategies(self, page, index: int):
        strategies = [
            ('checkbox_click', lambda: self._find_and_click_checkbox(page, index)),
            ('direct_widget', lambda: self._safe_click(page, '.cf-turnstile', index)),
            ('iframe_click', lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ('js_click', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('sitekey_attr', lambda: self._safe_click(page, '[data-sitekey]', index)),
            ('any_turnstile', lambda: self._safe_click(page, '*[class*="turnstile"]', index)),
            ('xpath_click', lambda: self._safe_click(page, "//div[@class='cf-turnstile']", index))
        ]
        
        for strategy_name, strategy_func in strategies:
            try:
                result = await strategy_func()
                if result is True or result is None:  # None означает успех для большинства стратегий
                    if self.debug:
                        logger.debug(f"Browser {index}: Click strategy '{strategy_name}' succeeded")
                    return True
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Click strategy '{strategy_name}' failed: {str(e)}")
                continue
        
        return False

    async def _safe_click(self, page, selector: str, index: int):
        """Полностью безопасный клик с максимальной защитой от ошибок"""
        try:
            # Пробуем кликнуть напрямую без count() проверки
            locator = page.locator(selector).first
            await locator.click(timeout=1000)
            return True
        except Exception as e:
            # Логируем ошибку только в debug режиме
            if self.debug and "Can't query n-th element" not in str(e):
                logger.debug(f"Browser {index}: Safe click failed for '{selector}': {str(e)}")
            return False

    async def _inject_captcha_directly(self, page, websiteKey: str, action: str = '', cdata: str = '', index: int = 0):
        """Inject CAPTCHA directly into the target website"""
        script = f"""
        // Remove any existing turnstile widgets first
        document.querySelectorAll('.cf-turnstile').forEach(el => el.remove());
        document.querySelectorAll('[data-sitekey]').forEach(el => el.remove());
        
        // Create turnstile widget directly on the page
        const captchaDiv = document.createElement('div');
        captchaDiv.className = 'cf-turnstile';
        captchaDiv.setAttribute('data-sitekey', '{websiteKey}');
        captchaDiv.setAttribute('data-callback', 'onTurnstileCallback');
        {f'captchaDiv.setAttribute("data-action", "{action}");' if action else ''}
        {f'captchaDiv.setAttribute("data-cdata", "{cdata}");' if cdata else ''}
        captchaDiv.style.position = 'fixed';
        captchaDiv.style.top = '20px';
        captchaDiv.style.left = '20px';
        captchaDiv.style.zIndex = '9999';
        captchaDiv.style.backgroundColor = 'white';
        captchaDiv.style.padding = '15px';
        captchaDiv.style.border = '2px solid #0f79af';
        captchaDiv.style.borderRadius = '8px';
        captchaDiv.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.3)';
        
        // Add to body immediately
        document.body.appendChild(captchaDiv);
        
        // Load Turnstile script and render widget
        const loadTurnstile = () => {{
            const script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
            script.async = true;
            script.defer = true;
            script.onload = function() {{
                console.log('Turnstile script loaded');
                // Wait a bit for script to initialize
                setTimeout(() => {{
                    if (window.turnstile && window.turnstile.render) {{
                        try {{
                            window.turnstile.render(captchaDiv, {{
                                sitekey: '{websiteKey}',
                                {f'action: "{action}",' if action else ''}
                                {f'cdata: "{cdata}",' if cdata else ''}
                                callback: function(token) {{
                                    console.log('Turnstile solved with token:', token);
                                    // Create hidden input for token
                                    let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                                    if (!tokenInput) {{
                                        tokenInput = document.createElement('input');
                                        tokenInput.type = 'hidden';
                                        tokenInput.name = 'cf-turnstile-response';
                                        document.body.appendChild(tokenInput);
                                    }}
                                    tokenInput.value = token;
                                }},
                                'error-callback': function(error) {{
                                    console.log('Turnstile error:', error);
                                }}
                            }});
                        }} catch (e) {{
                            console.log('Turnstile render error:', e);
                        }}
                    }} else {{
                        console.log('Turnstile API not available');
                    }}
                }}, 1000);
            }};
            script.onerror = function() {{
                console.log('Failed to load Turnstile script');
            }};
            document.head.appendChild(script);
        }};
        
        // Check if Turnstile is already loaded
        if (window.turnstile) {{
            console.log('Turnstile already loaded, rendering immediately');
            try {{
                window.turnstile.render(captchaDiv, {{
                    sitekey: '{websiteKey}',
                    {f'action: "{action}",' if action else ''}
                    {f'cdata: "{cdata}",' if cdata else ''}
                    callback: function(token) {{
                        console.log('Turnstile solved with token:', token);
                        let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                        if (!tokenInput) {{
                            tokenInput = document.createElement('input');
                            tokenInput.type = 'hidden';
                            tokenInput.name = 'cf-turnstile-response';
                            document.body.appendChild(tokenInput);
                        }}
                        tokenInput.value = token;
                    }},
                    'error-callback': function(error) {{
                        console.log('Turnstile error:', error);
                    }}
                }});
            }} catch (e) {{
                console.log('Immediate render error:', e);
                loadTurnstile();
            }}
        }} else {{
            loadTurnstile();
        }}
        
        // Setup global callback
        window.onTurnstileCallback = function(token) {{
            console.log('Global turnstile callback executed:', token);
        }};
        """

        await page.evaluate(script)
        if self.debug:
            logger.debug(f"Browser {index}: Injected CAPTCHA directly into website with sitekey: {websiteKey}")

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: Optional[str] = None, cdata: Optional[str] = None):
        """Solve the Turnstile challenge."""
        proxy = None
        context = None
        page = None
        browser_recycled = False

        index, browser, browser_config = await self.browser_pool.get()
        
        try:
            if hasattr(browser, 'is_connected') and not browser.is_connected():
                if self.debug:
                    logger.warning(f"Browser {index}: Browser disconnected, skipping")
                await self._replace_browser(index, browser, browser_config, reason="disconnected before solve")
                browser_recycled = True
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                return
        except Exception as e:
            if self.debug:
                logger.warning(f"Browser {index}: Cannot check browser state: {str(e)}")

        if self.proxy_support or self.proxy_url:
            proxy = self._get_browser_proxy()

            if proxy:
                proxy_config = self._parse_proxy(proxy)
                if self.debug:
                    logger.debug(f"Browser {index}: Creating context with proxy {proxy_config.get('server', proxy)}")
                context_options = {
                    "proxy": proxy_config,
                    "user_agent": browser_config['useragent']
                }

                if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                    context_options['extra_http_headers'] = {
                        'sec-ch-ua': browser_config['sec_ch_ua']
                    }

                context = await browser.new_context(**context_options)
            else:
                if self.debug:
                    logger.debug(f"Browser {index}: Creating context without proxy")
                context_options = {"user_agent": browser_config['useragent']}

                if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                    context_options['extra_http_headers'] = {
                        'sec-ch-ua': browser_config['sec_ch_ua']
                    }

                context = await browser.new_context(**context_options)
        else:
            context_options = {"user_agent": browser_config['useragent']}
            
            if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                context_options['extra_http_headers'] = {
                    'sec-ch-ua': browser_config['sec_ch_ua']
                }
            
            context = await browser.new_context(**context_options)

        page = await context.new_page()
        
        await self._antishadow_inject(page)
        
        await self._block_rendering(page)
        
        await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });
        
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
        };
        """)
        
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            await page.set_viewport_size({"width": 500, "height": 100})
            if self.debug:
                logger.debug(f"Browser {index}: Set viewport size to 500x240")

        start_time = time.time()

        try:
            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile solve for URL: {url} with Sitekey: {sitekey} | Action: {action} | Cdata: {cdata} | Proxy: {proxy}")
                logger.debug(f"Browser {index}: Setting up optimized page loading with resource blocking")

            if self.debug:
                logger.debug(f"Browser {index}: Loading real website directly: {url}")

            await page.goto(url, wait_until='domcontentloaded', timeout=30000)

            await self._unblock_rendering(page)

            # Сразу инъектируем виджет Turnstile на целевой сайт
            if self.debug:
                logger.debug(f"Browser {index}: Injecting Turnstile widget directly into target site")
            
            await self._inject_captcha_directly(page, sitekey, action or '', cdata or '', index)
            
            # Ждем время для загрузки и рендеринга виджета
            await asyncio.sleep(3)

            locator = page.locator('input[name="cf-turnstile-response"]')
            max_attempts = 30
            click_count = 0
            max_clicks = 10

            for attempt in range(max_attempts):
                try:
                    # Безопасная проверка количества элементов с токеном
                    try:
                        count = await locator.count()
                    except Exception as e:
                        if self.debug:
                            logger.debug(f"Browser {index}: Locator count failed on attempt {attempt + 1}: {str(e)}")
                        count = 0

                    if count == 0:
                        if self.debug and attempt % 5 == 0:
                            logger.debug(f"Browser {index}: No token elements found on attempt {attempt + 1}")
                    elif count == 1:
                        # Если только один элемент, проверяем его токен
                        try:
                            token = await locator.input_value(timeout=500)
                            if token:
                                elapsed_time = round(time.time() - start_time, 3)
                                logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                await save_result(task_id, "turnstile", {"value": token, "elapsed_time": elapsed_time})
                                return
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Single token element check failed: {str(e)}")
                    else:
                        # Если несколько элементов, проверяем все по очереди
                        if self.debug:
                            logger.debug(f"Browser {index}: Found {count} token elements, checking all")

                        for i in range(count):
                            try:
                                element_token = await locator.nth(i).input_value(timeout=500)
                                if element_token:
                                    elapsed_time = round(time.time() - start_time, 3)
                                    logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{element_token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                    await save_result(task_id, "turnstile", {"value": element_token, "elapsed_time": elapsed_time})
                                    return
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Token element {i} check failed: {str(e)}")
                                continue

                    if attempt > 2 and attempt % 3 == 0 and click_count < max_clicks:
                        click_success = await self._try_click_strategies(page, index)
                        click_count += 1
                        if click_success and self.debug:
                            logger.debug(f"Browser {index}: Click successful (click #{click_count}/{max_clicks})")
                        elif not click_success and self.debug:
                            logger.debug(f"Browser {index}: All click strategies failed on attempt {attempt + 1} (click #{click_count}/{max_clicks})")

                    # Адаптивное ожидание
                    wait_time = min(0.5 + (attempt * 0.05), 2.0)
                    await asyncio.sleep(wait_time)

                    if self.debug and attempt % 5 == 0:
                        logger.debug(f"Browser {index}: Attempt {attempt + 1}/{max_attempts} - Waiting for token (clicks: {click_count}/{max_clicks})")

                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Attempt {attempt + 1} error: {str(e)}")
                    continue
            
            elapsed_time = round(time.time() - start_time, 3)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time})
            if self.debug:
                logger.error(f"Browser {index}: Error solving Turnstile in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds")
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time})
            if self.debug:
                logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Closing browser context and cleaning up")
            
            if context:
                try:
                    await context.close()
                    if self.debug:
                        logger.debug(f"Browser {index}: Context closed successfully")
                except Exception as e:
                    if self.debug:
                        logger.warning(f"Browser {index}: Error closing context: {str(e)}")

            if not browser_recycled:
                await self._return_browser(index, browser, browser_config)






    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')

        if not url or not sitekey:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PAGEURL",
                "errorDescription": "Both 'url' and 'sitekey' are required"
            }), 200

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata
        })

        try:
            asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata))

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({
                "errorId": 0,
                "taskId": task_id
            }), 200
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e)
            }), 200

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        if not result:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Task not found"
            }), 200

        if result == "CAPTCHA_NOT_READY" or (isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY"):
            return jsonify({"status": "processing"}), 200

        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }), 200

        if isinstance(result, dict) and result.get("value") and result.get("value") != "CAPTCHA_FAIL":
            solution = {"token": result["value"]}
            # 透传 grok_setup / cf_clearance 的额外字段
            for key in ("sso", "sso_rw", "birth_ok", "nsfw_ok", "cf_clearance", "user_agent"):
                if key in result:
                    solution[key] = result[key]
            return jsonify({
                "errorId": 0,
                "status": "ready",
                "solution": solution
            }), 200
        else:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }), 200



    async def grok_setup(self):
        """Handle /grok_setup - set birth_date and nsfw via browser."""
        data = await request.get_json() or {}
        sso = data.get('sso', '')
        sso_rw = data.get('sso_rw', '')
        verify_url = data.get('verify_url', '')
        accounts_cookies = data.get('accounts_cookies', {})
        task_id = str(uuid.uuid4())
        await save_result(
            task_id,
            "grok_setup",
            {"status": "CAPTCHA_NOT_READY", "createTime": time.time()},
        )
        asyncio.create_task(self._do_grok_setup(task_id, sso, sso_rw, verify_url, accounts_cookies))
        return jsonify({"errorId": 0, "taskId": task_id}), 200

    async def _do_grok_setup(self, task_id: str, sso: str, sso_rw: str, verify_url: str = "", accounts_cookies: Optional[dict] = None):
        import base64, datetime, random as _rnd
        nsfw_raw = (
            b"\x00\x00\x00\x00\x20\x0a\x02\x10\x01\x12\x1a\x0a\x18"
            b"always_show_nsfw_content"
        )
        nsfw_b64 = base64.b64encode(nsfw_raw).decode()

        index, browser, browser_config = await self.browser_pool.get()
        context = None
        page = None
        try:
            ctx_opts = {}
            ua = (browser_config or {}).get('useragent') or ''
            if ua:
                ctx_opts['user_agent'] = ua
            context = await browser.new_context(**ctx_opts)
            page = await context.new_page()

            browser_sso = sso or None
            browser_sso_rw = sso_rw or None
            birth_ok = False
            nsfw_ok = False

            if verify_url:
                if accounts_cookies:
                    xai_cookies = [
                        {"name": k, "value": v, "domain": ".x.ai", "path": "/"}
                        for k, v in accounts_cookies.items()
                    ]
                    await context.add_cookies(xai_cookies)

                logger.info("grok_setup: navigating verify_url via browser")
                try:
                    await page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(8)
                except Exception as nav_e:
                    logger.warning(f"grok_setup: verify_url navigation error: {nav_e}")

                current_url = page.url
                logger.info(f"grok_setup: after verify_url, current_url={current_url}")

                all_cookies = await context.cookies("https://grok.com")
                for c in all_cookies:
                    if c["name"] == "sso":
                        browser_sso = c["value"]
                    elif c["name"] == "sso-rw":
                        browser_sso_rw = c["value"]
                logger.info(f"grok_setup: browser cookies - sso={bool(browser_sso)}, sso_rw={bool(browser_sso_rw)}")

                # 在 accounts.x.ai 页面完成所有操作（TOS + birth_date + nsfw）
                # accounts.x.ai 的 Cloudflare 允许此 IP，grok.com 不允许
                if "accounts.x.ai" in current_url or "accept-tos" in current_url:
                    today = datetime.date.today()
                    age = _rnd.randint(20, 40)
                    birth_date = f"{today.year - age}-{_rnd.randint(1,12):02d}-{_rnd.randint(1,28):02d}T16:00:00.000Z"

                    setup_result = await page.evaluate(f"""
                        async () => {{
                            const results = {{}};
                            // 1. 接受 TOS
                            try {{
                                const tosData = new Uint8Array([0x00, 0x00, 0x00, 0x00, 0x02, 0x10, 0x01]);
                                const tosR = await fetch('/auth_mgmt.AuthManagement/SetTosAcceptedVersion', {{
                                    method: 'POST',
                                    headers: {{'content-type': 'application/grpc-web+proto', 'x-grpc-web': '1'}},
                                    body: tosData
                                }});
                                results.tos = {{status: tosR.status, ok: tosR.ok}};
                            }} catch(e) {{ results.tos = {{status: 0, ok: false, error: String(e)}}; }}

                            // 2. 设置 birth_date（尝试 accounts.x.ai）
                            try {{
                                const birthR = await fetch('/rest/auth/set-birth-date', {{
                                    method: 'POST',
                                    headers: {{'content-type': 'application/json'}},
                                    body: JSON.stringify({{birthDate: '{birth_date}'}})
                                }});
                                results.birth = {{status: birthR.status, ok: birthR.ok}};
                            }} catch(e) {{ results.birth = {{status: 0, ok: false, error: String(e)}}; }}

                            // 3. 设置 nsfw（尝试 accounts.x.ai）
                            try {{
                                const raw = atob('{nsfw_b64}');
                                const nsfwData = new Uint8Array(raw.length);
                                for (let i = 0; i < raw.length; i++) nsfwData[i] = raw.charCodeAt(i);
                                const nsfwR = await fetch('/auth_mgmt.AuthManagement/UpdateUserFeatureControls', {{
                                    method: 'POST',
                                    headers: {{'content-type': 'application/grpc-web+proto', 'x-grpc-web': '1'}},
                                    body: nsfwData
                                }});
                                results.nsfw = {{status: nsfwR.status, ok: nsfwR.ok}};
                            }} catch(e) {{ results.nsfw = {{status: 0, ok: false, error: String(e)}}; }}

                            return results;
                        }}
                    """)
                    logger.info(f"grok_setup: accounts.x.ai setup result: {setup_result}")
                    birth_ok = bool((setup_result or {}).get('birth', {}).get('ok'))
                    nsfw_ok = bool((setup_result or {}).get('nsfw', {}).get('ok'))

            logger.info(f"grok_setup done: birth={birth_ok}, nsfw={nsfw_ok}, sso={bool(browser_sso)}")
            await save_result(task_id, "grok_setup", {
                "value": "done",
                "birth_ok": birth_ok,
                "nsfw_ok": nsfw_ok,
                "sso": browser_sso or "",
                "sso_rw": browser_sso_rw or "",
            })
        except Exception as e:
            logger.error(f"grok_setup error: {e}")
            await save_result(task_id, "grok_setup", {"value": "CAPTCHA_FAIL", "error": str(e)})
        finally:
            if page:
                try: await page.close()
                except: pass
            if context:
                try: await context.close()
                except: pass
            await self._return_browser(index, browser, browser_config)

    async def cf_clearance_handler(self):
        """Handle /cf_clearance - obtain cf_clearance cookie via browser."""
        task_id = str(uuid.uuid4())
        await save_result(
            task_id,
            "cf_clearance",
            {"status": "CAPTCHA_NOT_READY", "createTime": time.time()},
        )
        asyncio.create_task(self._do_cf_clearance(task_id))
        return jsonify({"errorId": 0, "taskId": task_id}), 200

    async def _do_cf_clearance(self, task_id: str) -> None:
        """Navigate to grok.com, wait for CF challenge to resolve, extract cf_clearance cookie."""
        index, browser, browser_config = await self.browser_pool.get()
        context = None
        page = None
        try:
            # Build context options (with proxy if enabled)
            # Let camoufox use its natural UA/fingerprint for best CF compatibility
            ctx_opts: dict = {}
            ua = (browser_config or {}).get('useragent') or ''
            if ua:
                ctx_opts['user_agent'] = ua

            if self.proxy_support or self.proxy_url:
                proxy = self._get_browser_proxy()
                if proxy:
                    ctx_opts['proxy'] = self._parse_proxy(proxy)

            context = await browser.new_context(**ctx_opts)
            page = await context.new_page()

            # Hide webdriver property
            await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)

            logger.info("cf_clearance: navigating to https://grok.com ...")
            response = None
            try:
                response = await page.goto("https://grok.com", wait_until="domcontentloaded", timeout=60000)
            except Exception as nav_e:
                logger.warning(f"cf_clearance: navigation error (may be expected during challenge): {nav_e}")

            # Diagnose what CF returned
            status_code = response.status if response else "N/A"
            try:
                page_title = await page.title()
            except Exception:
                page_title = "N/A"
            frame_urls = [f.url for f in page.frames if f != page.main_frame]
            has_challenge_frame = any("challenges.cloudflare.com" in u for u in frame_urls)
            logger.info(
                f"cf_clearance: page loaded - status={status_code}, title='{page_title}', "
                f"frames={len(frame_urls)}, has_challenge_iframe={has_challenge_frame}"
            )
            if frame_urls:
                for fu in frame_urls[:5]:
                    logger.info(f"cf_clearance: iframe url: {fu}")

            # If page returned 403, try to extract page text for debugging
            if status_code == 403:
                try:
                    body_text = await page.evaluate("document.body?.innerText?.substring(0, 500)")
                    logger.info(f"cf_clearance: 403 page body: {body_text}")
                except Exception:
                    pass

            # Give the page a moment to render the challenge
            await asyncio.sleep(5)

            # Re-check frames after waiting (challenge iframe may load async)
            frame_urls_after = [f.url for f in page.frames if f != page.main_frame]
            has_challenge_after = any("challenges.cloudflare.com" in u for u in frame_urls_after)
            if has_challenge_after and not has_challenge_frame:
                logger.info("cf_clearance: challenge iframe appeared after wait")
                for fu in frame_urls_after[:5]:
                    logger.info(f"cf_clearance: iframe url: {fu}")

            # Wait for CF challenge to resolve (up to 120 seconds)
            cf_cookie_value: Optional[str] = None
            max_wait = 120
            for i in range(max_wait):
                # Try clicking the CF challenge checkbox/button if present
                if i % 5 == 3:
                    try:
                        for frame in page.frames:
                            if "challenges.cloudflare.com" in (frame.url or ""):
                                # Try checkbox
                                try:
                                    checkbox = frame.locator("input[type='checkbox']")
                                    if await checkbox.count() > 0:
                                        await checkbox.first.click(timeout=2000)
                                        logger.info("cf_clearance: clicked challenge checkbox")
                                except Exception:
                                    pass
                                # Try the verify button / challenge body
                                try:
                                    body = frame.locator("body")
                                    if await body.count() > 0:
                                        box = await body.bounding_box()
                                        if box:
                                            # Click center of challenge iframe
                                            await frame.click("body", position={"x": box["width"] / 2, "y": box["height"] / 2}, timeout=2000)
                                            logger.info("cf_clearance: clicked challenge body center")
                                except Exception:
                                    pass
                    except Exception:
                        pass

                await asyncio.sleep(1)
                cookies = await context.cookies("https://grok.com")
                for c in cookies:
                    if c["name"] == "cf_clearance":
                        cf_cookie_value = c["value"]
                        break
                if cf_cookie_value:
                    break
                if i % 15 == 14:
                    try:
                        current_url = page.url
                        cur_title = await page.title()
                        cur_frames = [f.url for f in page.frames if f != page.main_frame and "challenges" in (f.url or "")]
                        logger.info(f"cf_clearance: waiting... ({i + 1}s, url={current_url}, title='{cur_title}', cf_frames={len(cur_frames)})")
                    except Exception:
                        logger.info(f"cf_clearance: waiting for cookie... ({i + 1}s)")

            if cf_cookie_value:
                # Capture the actual UA used by the browser
                try:
                    actual_ua = await page.evaluate("navigator.userAgent")
                except Exception:
                    actual_ua = ""
                logger.info(f"cf_clearance: obtained cookie ({cf_cookie_value[:16]}...), ua={actual_ua[:40]}...")
                await save_result(task_id, "cf_clearance", {
                    "value": "done",
                    "cf_clearance": cf_cookie_value,
                    "user_agent": actual_ua or "",
                })
            else:
                logger.warning("cf_clearance: cookie not found after waiting")
                await save_result(task_id, "cf_clearance", {
                    "value": "CAPTCHA_FAIL",
                    "error": "cf_clearance cookie not found within timeout",
                })
        except Exception as e:
            logger.error(f"cf_clearance error: {e}")
            await save_result(task_id, "cf_clearance", {"value": "CAPTCHA_FAIL", "error": str(e)})
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            await self._return_browser(index, browser, browser_config)

    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Welcome to Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">To use the turnstile service, send a GET request to 
                       <code class="bg-red-700 text-white px-2 py-1 rounded">/turnstile</code> with the following query parameters:</p>

                    <ul class="list-disc pl-6 mb-6 text-gray-300">
                        <li><strong>url</strong>: The URL where Turnstile is to be validated</li>
                        <li><strong>sitekey</strong>: The site key for Turnstile</li>
                    </ul>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example usage:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=sitekey</code>
                    </div>


                    <div class="bg-gray-700 p-4 rounded-lg mb-6">
                        <p class="text-gray-200 font-semibold mb-3">📢 Connect with Us</p>
                        <div class="space-y-2 text-sm">
                            <p class="text-gray-300">
                                📢 <strong>Channel:</strong> 
                                <a href="https://t.me/D3_vin" class="text-red-300 hover:underline">https://t.me/D3_vin</a> 
                                - Latest updates and releases
                            </p>
                            <p class="text-gray-300">
                                💬 <strong>Chat:</strong> 
                                <a href="https://t.me/D3vin_chat" class="text-red-300 hover:underline">https://t.me/D3vin_chat</a> 
                                - Community support and discussions
                            </p>
                            <p class="text-gray-300">
                                📁 <strong>GitHub:</strong> 
                                <a href="https://github.com/D3-vin" class="text-red-300 hover:underline">https://github.com/D3-vin</a> 
                                - Source code and development
                            </p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--no-headless', action='store_true', help='Run the browser with GUI (disable headless mode). By default, headless mode is enabled.')
    parser.add_argument('--useragent', type=str, help='User-Agent string (if not specified, random configuration is used)')
    parser.add_argument('--debug', action='store_true', help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=4, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--proxy-url', type=str, default='', help='Proxy URL to use (e.g., http://127.0.0.1:7897). Implies --proxy.')
    parser.add_argument('--random', action='store_true', help='Use random User-Agent and Sec-CH-UA configuration from pool')
    parser.add_argument('--browser', type=str, help='Specify browser name to use (e.g., chrome, firefox)')
    parser.add_argument('--version', type=str, help='Specify browser version to use (e.g., 139, 141)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5072', help='Set the port for the API solver to listen on. (Default: 5072)')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool, browser_name: str, browser_version: str, proxy_url: str = "") -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support, use_random_config=use_random_config, browser_name=browser_name, browser_version=browser_version, proxy_url=proxy_url)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    # --proxy-url implies --proxy
    proxy_url = (args.proxy_url or "").strip()
    proxy_support = args.proxy or bool(proxy_url)
    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    else:
        app = create_app(
            headless=not args.no_headless,
            debug=args.debug,
            useragent=args.useragent,
            browser_type=args.browser_type,
            thread=args.thread,
            proxy_support=proxy_support,
            use_random_config=args.random,
            browser_name=args.browser,
            browser_version=args.version,
            proxy_url=proxy_url,
        )
        app.run(host=args.host, port=int(args.port))
