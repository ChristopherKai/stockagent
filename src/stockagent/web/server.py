"""stock web：FastAPI 应用装配、配置热重载、后台任务执行器与调度器的生命周期。

只监听 127.0.0.1。页面只读数据库，不联网（行情、公告的抓取都在后台任务里做）。
"""

from __future__ import annotations

import shutil
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import chat, config, jobs, scheduler, tracker, triage, valuation
from ..app import MARKET_LABEL, SOURCE_LABEL, App
from ..db import DB
from ..jobs import JobRunner
from ..llm import LLM
from ..sources import web as websrc
from ..utils import log
from . import render
from .security import CSRF_FIELD, LOOPBACK_HOSTS, csrf_token, security_middleware

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
render.register(templates.env)
templates.env.globals.update(
    TARGET_LABEL=valuation.TARGET_LABEL, DISCLAIMER=tracker.DISCLAIMER, SCENARIO_LABEL=valuation.SCENARIO_LABEL,
    KIND_LABEL=tracker.KIND_LABEL, THESIS_STATUS=tracker.THESIS_STATUS, STANCE_LABEL=tracker.STANCE_LABEL,
    IMPACT_LABEL=tracker.IMPACT_LABEL, TRIGGER_LABEL=tracker.TRIGGER_LABEL, DIRECTIONS=triage.DIRECTIONS,
    EVENT_TYPES=triage.EVENT_TYPES, SOURCE_LABEL=SOURCE_LABEL, MARKET_LABEL=MARKET_LABEL, TASKS=jobs.TASKS,
    OP_LABEL=tracker.OP_LABEL, THESIS_HELP=tracker.THESIS_HELP, TASK_CATEGORIES=jobs.CATEGORIES, FLAG_LABEL=jobs.FLAG_LABEL,
)


class AppHolder:
    """当前的 App（配置、观察清单、数据库、模型）。保存配置后 reload：复用数据库连接，重建 LLM（清掉客户端缓存）。
    后台任务（如「新增公司」）改了 watchlist.yaml 时，下次取 app 自动重新加载。"""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        self._lock = threading.RLock()
        self._app = App.load(self.config_path)
        self._wl_mtime = self._watchlist_mtime()

    def _watchlist_mtime(self) -> int | None:
        try:
            return self.watchlist_path.stat().st_mtime_ns
        except OSError:
            return None

    @property
    def app(self) -> App:
        mtime = self._watchlist_mtime()
        if mtime != self._wl_mtime:
            try:
                self.reload()
            except Exception as e:  # noqa: BLE001 - 文件改坏了就继续用旧的，页面照常工作
                log.warning("watchlist.yaml 已修改但无法加载，继续使用旧的观察清单：%s", e)
            self._wl_mtime = mtime
        return self._app

    @property
    def watchlist_path(self) -> Path:
        return self.root / "watchlist.yaml"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def backup_dir(self) -> Path:
        return self._app.data_dir / "backups"

    def reload(self) -> None:
        with self._lock:
            settings, watchlist, root = config.load(self.config_path)
            old = self._app
            db = old.db if settings.data_dir == old.settings.data_dir else DB(settings.data_dir / "stock.db")
            self._app = App(settings, watchlist, root, db, LLM(settings.llm, db))
            self._wl_mtime = self._watchlist_mtime()


def page(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    holder: AppHolder = request.app.state.holder
    token = csrf_token(request)
    ctx.update(request=request, app=holder.app, csrf=token,
               csrf_field=Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{escape(token)}">'),
               red_up=holder.app.settings.report.color_scheme == "red_up")
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def create_app(config_path: str | Path = "config.yaml", allowed_hosts: list[str] | None = None,
               start_background: bool = True) -> FastAPI:
    """start_background：启动任务执行器与调度器（测试里关闭）。同一台机器只有一个 stock web 进程会运行它们（web 锁）。"""
    holder = AppHolder(config_path)
    runner = JobRunner(holder)
    sched = scheduler.Scheduler(holder)
    chat_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chat")  # 报告研读对话的回答在这里生成

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stack = ExitStack()
        stack.callback(lambda: chat_pool.shutdown(wait=False, cancel_futures=True))
        if start_background:
            if stack.enter_context(holder.app.lock("web")):
                holder.app.db.fail_pending_chat_messages(chat.INTERRUPTED)  # 上次退出时没写完的回答
                runner.start()
                sched.start()
                stack.callback(runner.stop)
                stack.callback(sched.stop)
            else:
                log.warning("已有 stock web 在运行：本进程不启动任务执行器与调度器")
        try:
            yield
        finally:
            stack.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, title="stockagent")
    app.state.holder = holder
    app.state.runner = runner
    app.state.chat_pool = chat_pool
    app.middleware("http")(security_middleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOOPBACK_HOSTS)  # 最外层：先挡掉非本机 Host
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    from . import chat_views, screen_views, settings_views, views  # noqa: PLC0415 - 路由模块引用本模块的 page()

    app.include_router(views.router)
    app.include_router(chat_views.router)
    app.include_router(settings_views.router)
    app.include_router(screen_views.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if "text/html" in request.headers.get("accept", ""):
            return page(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    return app


def serve(config_path: str | Path, port: int | None = None, background: bool = True, open_browser: bool = False) -> None:
    import uvicorn  # noqa: PLC0415 - 只有 stock web 需要

    app = create_app(config_path, start_background=background)
    holder: AppHolder = app.state.holder
    port = port or holder.app.settings.web.port
    if websrc.MCPORTER is None and shutil.which("mcporter", path="/opt/homebrew/bin:/usr/local/bin"):
        print("提示：PATH 中找不到 mcporter（Exa 搜索会不可用）。用 launchd / cron 启动时，请在环境变量 PATH 里加上 /opt/homebrew/bin")
    url = f"http://127.0.0.1:{port}/"
    print(f"stockagent WebUI：{url}（只监听本机；Ctrl+C 退出）")
    if not holder.app.settings.scheduler.enabled:
        print("内置调度器已关闭（config.yaml 的 scheduler.enabled）")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, log_level="warning")
