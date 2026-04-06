from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Error, Page, Playwright

from .config import Settings


@dataclass
class BrowserSession:
    context: BrowserContext
    browser: Optional[Browser] = None
    managed: bool = True

    def close(self) -> None:
        if not self.managed:
            return
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self.browser is not None:
                self.browser.close()


def open_browser_session(playwright: Playwright, settings: Settings) -> BrowserSession:
    if settings.browser_cdp_url:
        browser = playwright.chromium.connect_over_cdp(settings.browser_cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError(
                f"No browser context found at {settings.browser_cdp_url}. Start Chrome with --remote-debugging-port first."
            )
        return BrowserSession(context=contexts[0], browser=browser, managed=False)

    launch_options = {"headless": settings.headless}
    if settings.browser_channel:
        launch_options["channel"] = settings.browser_channel

    viewport = {"width": 1720, "height": 1200}
    if settings.browser_profile_dir:
        settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            str(settings.browser_profile_dir),
            viewport=viewport,
            accept_downloads=True,
            **launch_options,
        )
        return BrowserSession(context=context)

    browser = playwright.chromium.launch(**launch_options)
    context = browser.new_context(viewport=viewport, accept_downloads=True)
    return BrowserSession(context=context, browser=browser)


def prepare_work_page(context: BrowserContext) -> Page:
    pages = list(context.pages)
    if not pages:
        return context.new_page()

    primary = pages[0]
    for page in pages[1:]:
        try:
            page.close()
        except Error:
            continue
    return primary
