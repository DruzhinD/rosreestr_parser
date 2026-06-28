"""
Парсер Росреестра — получение «Вида объекта недвижимости» по кадастровому номеру.

Использует Selenium (Chrome) + ddddocr для решения капчи без внешних сервисов.

Запуск:
    python parser.py 23:37:0801002:400
    python parser.py 23:37:0801002:400 --headless
    python parser.py 23:37:0801002:400 --retries 10 --debug
"""

import argparse
import base64
import io
import sys
import time
from pathlib import Path
from typing import Optional

# UTF-8 вывод на Windows-консоли
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ddddocr
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

TARGET_URL = "https://lk.rosreestr.ru/eservices/real-estate-objects-online"

_SUBMIT_ID = "realestateobjects-search"

# Fallback-селекторы на случай изменения страницы
_KN_FALLBACK = [
    "input#query",
    "input[name='query']",
    "input[placeholder*='адастровый']",
    "input[placeholder*='адрес']",
]
_CAPTCHA_IMG_FALLBACK = [
    ".rros-ui-lib-captcha-content img",
    "img[src*='captcha']",
    "img[class*='captcha']",
    ".captcha img",
]
_CAPTCHA_INPUT_FALLBACK = [
    "input#captcha",
    "input[name='captcha']",
    "input.rros-ui-lib-captcha-input",
    "input[placeholder*='имволы']",
    "input[placeholder*='Введ']",
]

# Fallback XPath для классических (не-React) таблиц
_OBJECT_TYPE_XPATHS_FALLBACK = [
    "//td[contains(., 'Вид объекта')]/following-sibling::td[1]",
    "//th[contains(., 'Вид объекта')]/following-sibling::td[1]",
    "//dt[contains(., 'Вид объекта')]/following-sibling::dd[1]",
    "//*[contains(text(),'Вид объекта')]/following-sibling::*[1]",
]

# JS для React-совместимого заполнения <input>
_JS_SET_VALUE = """
var el = arguments[0], val = arguments[1];
var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
setter.call(el, val);
el.dispatchEvent(new Event('input',  { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
"""


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------

def _build_driver(headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


# ---------------------------------------------------------------------------
# Элементы
# ---------------------------------------------------------------------------

def _find(driver: webdriver.Chrome, selectors: list[str]) -> Optional[webdriver.remote.webelement.WebElement]:
    for sel in selectors:
        try:
            return driver.find_element(By.CSS_SELECTOR, sel)
        except NoSuchElementException:
            pass
    return None


def _wait_for(driver: webdriver.Chrome, selectors: list[str], timeout: float = 15) -> Optional[webdriver.remote.webelement.WebElement]:
    """Ждёт появления любого из CSS-селекторов (суммарно до timeout секунд)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        el = _find(driver, selectors)
        if el is not None:
            return el
        time.sleep(0.5)
    return None


def _react_fill(driver: webdriver.Chrome, element: webdriver.remote.webelement.WebElement, value: str) -> None:
    """Заполняет React-controlled <input> так, чтобы компонент обновил state."""
    # Сначала кликаем и очищаем стандартным способом
    element.click()
    element.clear()
    # Затем устанавливаем значение через нативный сеттер + события
    driver.execute_script(_JS_SET_VALUE, element, value)
    time.sleep(0.2)


# ---------------------------------------------------------------------------
# Капча
# ---------------------------------------------------------------------------

def _get_captcha_image(driver: webdriver.Chrome) -> Image.Image:
    img_el = _find(driver, _CAPTCHA_IMG_FALLBACK)
    if img_el is None:
        raise RuntimeError("Изображение капчи не найдено")

    src: str = img_el.get_attribute("src") or ""
    if src.startswith("data:image"):
        raw = base64.b64decode(src.split(",", 1)[1])
        return Image.open(io.BytesIO(raw))

    # Скриншот области элемента
    loc = img_el.location_once_scrolled_into_view
    size = img_el.size
    dpr = driver.execute_script("return window.devicePixelRatio || 1")
    png = driver.get_screenshot_as_png()
    page_img = Image.open(io.BytesIO(png))
    x, y = int(loc["x"] * dpr), int(loc["y"] * dpr)
    w, h = int(size["width"] * dpr), int(size["height"] * dpr)
    return page_img.crop((x, y, x + w, y + h))


def _solve_captcha(img: Image.Image) -> str:
    ocr = ddddocr.DdddOcr(show_ad=False)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return ocr.classification(buf.getvalue()).strip()


# ---------------------------------------------------------------------------
# Состояние страницы
# ---------------------------------------------------------------------------

def _captcha_error(driver: webdriver.Chrome) -> bool:
    text = driver.find_element(By.TAG_NAME, "body").text.lower()
    return any(k in text for k in ("неверн", "captcha", "повторите", "ошибка ввода"))


def _object_not_found(driver: webdriver.Chrome) -> bool:
    text = driver.find_element(By.TAG_NAME, "body").text.lower()
    return any(k in text for k in ("не найден", "не обнаружен", "отсутствует", "not found"))


# ---------------------------------------------------------------------------
# Результат
# ---------------------------------------------------------------------------

def _extract_object_type(driver: webdriver.Chrome) -> Optional[str]:
    # 1. Динамически определяем индекс колонки «Вид объекта» по заголовку
    try:
        head_cells = driver.find_elements(
            By.XPATH, "//div[starts-with(@data-test-id,'head-cell-')]"
        )
        col_idx = None
        for hc in head_cells:
            title = hc.get_attribute("title") or hc.text
            if "Вид объекта" in title:
                col_idx = hc.get_attribute("data-test-id").split("-")[-1]
                break

        if col_idx is not None:
            cell = driver.find_element(
                By.XPATH, f"//div[@data-test-id='cell-{col_idx}']//a[normalize-space()]"
            )
            val = cell.text.strip()
            if val:
                return val
    except (NoSuchElementException, Exception):
        pass

    # 2. Fallback для классических HTML-таблиц
    for xpath in _OBJECT_TYPE_XPATHS_FALLBACK:
        try:
            el = driver.find_element(By.XPATH, xpath)
            val = el.text.strip()
            if val:
                return val
        except NoSuchElementException:
            pass

    return None


# ---------------------------------------------------------------------------
# Отладка
# ---------------------------------------------------------------------------

def _save_debug(driver: webdriver.Chrome, tag: str) -> None:
    driver.save_screenshot(f"debug_{tag}.png")
    Path(f"debug_{tag}.html").write_text(driver.page_source, encoding="utf-8")
    print(f"[debug] debug_{tag}.png  debug_{tag}.html")


# ---------------------------------------------------------------------------
# Парсинг
# ---------------------------------------------------------------------------

def parse(
    cadastral_number: str,
    headless: bool = False,
    max_retries: int = 7,
    debug: bool = False,
) -> Optional[str]:
    """
    Возвращает «Вид объекта недвижимости» или None если объект не найден.

    Raises:
        RuntimeError: при критических ошибках или исчерпании попыток.
    """
    driver = _build_driver(headless)
    try:
        driver.get(TARGET_URL)
        time.sleep(3)  # ждём SPA

        if debug:
            _save_debug(driver, "initial")

        for attempt in range(1, max_retries + 1):
            print(f"[{attempt}/{max_retries}] Заполняем форму...")

            # 1. Поле кадастрового номера
            kn = _wait_for(driver, _KN_FALLBACK, timeout=15)
            if kn is None:
                if debug:
                    _save_debug(driver, f"no_kn_{attempt}")
                raise RuntimeError("Поле кадастрового номера не найдено (используйте --debug).")
            _react_fill(driver, kn, cadastral_number)

            # 2. Решаем капчу
            try:
                cap_img = _get_captcha_image(driver)
                if debug:
                    cap_img.save(f"debug_captcha_{attempt}.png")
                cap_text = _solve_captcha(cap_img)
                print(f"       Капча -> '{cap_text}'")
            except Exception as exc:
                print(f"       Ошибка капчи: {exc}")
                if debug:
                    _save_debug(driver, f"captcha_err_{attempt}")
                time.sleep(1)
                continue

            # 3. Вводим капчу
            cap_in = _find(driver, _CAPTCHA_INPUT_FALLBACK)
            if cap_in is None:
                print("       Поле ввода капчи не найдено.")
                if debug:
                    _save_debug(driver, f"no_cap_in_{attempt}")
                time.sleep(1)
                continue
            _react_fill(driver, cap_in, cap_text)

            # 4. Ждём, пока кнопка станет активной (макс. 5 сек)
            submit = None
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    btn = driver.find_element(By.ID, _SUBMIT_ID)
                    if not btn.get_attribute("disabled"):
                        submit = btn
                        break
                except NoSuchElementException:
                    pass
                time.sleep(0.3)

            if submit is None:
                # Кнопка так и не стала активной — пробуем кликнуть через JS
                print("       Кнопка disabled, кликаем через JS...")
                try:
                    btn = driver.find_element(By.ID, _SUBMIT_ID)
                    driver.execute_script("arguments[0].click()", btn)
                except NoSuchElementException:
                    if debug:
                        _save_debug(driver, f"no_submit_{attempt}")
                    raise RuntimeError("Кнопка 'Найти' не найдена на странице.")
            else:
                submit.click()

            time.sleep(3)

            # 5. Проверяем результат
            if _captcha_error(driver):
                print("       Неверная капча, пробуем снова...")
                try:
                    reload = driver.find_element(
                        By.CSS_SELECTOR,
                        ".rros-ui-lib-captcha-content-reload-btn, [class*='reload-btn']",
                    )
                    reload.click()
                    time.sleep(1)
                except NoSuchElementException:
                    pass
                continue

            if _object_not_found(driver):
                print("Объект не найден по данному кадастровому номеру.")
                return None

            result = _extract_object_type(driver)
            if result:
                if debug:
                    _save_debug(driver, f"success_{attempt}")
                return result

            # Ответ пришёл, но структура не распознана
            if debug:
                _save_debug(driver, f"result_{attempt}")
                body = driver.find_element(By.TAG_NAME, "body").text
                print("[debug] Текст страницы (3000 симв.):")
                print(body[:3000])
            else:
                driver.save_screenshot(f"debug_result_{attempt}.png")
                print(f"       'Вид объекта' не найден. Скриншот: debug_result_{attempt}.png")

    finally:
        driver.quit()

    raise RuntimeError(f"Не удалось получить данные за {max_retries} попыток.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Парсер Росреестра: «Вид объекта недвижимости» по кадастровому номеру"
    )
    ap.add_argument("cadastral_number", help="Напр.: 23:37:0801002:400")
    ap.add_argument("--headless", action="store_true", help="Браузер без окна")
    ap.add_argument("--retries", type=int, default=7, help="Попыток (по умолчанию 7)")
    ap.add_argument("--debug", action="store_true",
                    help="Сохранять скриншоты/HTML и выводить диагностику")
    args = ap.parse_args()

    print(f"Кадастровый номер: {args.cadastral_number}")
    print(f"Режим: {'headless' if args.headless else 'с браузером'}, попыток: {args.retries}")
    print("-" * 60)

    try:
        result = parse(
            cadastral_number=args.cadastral_number,
            headless=args.headless,
            max_retries=args.retries,
            debug=args.debug,
        )
    except RuntimeError as exc:
        print(f"\nОшибка: {exc}")
        sys.exit(1)

    if result:
        print(f"\nВид объекта недвижимости: {result}")
    else:
        print("\nРезультат: объект не найден")
        sys.exit(1)


if __name__ == "__main__":
    main()
