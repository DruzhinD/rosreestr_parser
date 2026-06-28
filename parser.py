"""
Парсер Росреестра — пакетная обработка кадастровых номеров из input.csv.

Читает: input.csv  (колонка kadastral_number или первая колонка)
Пишет:  output.csv (kadastral_number, object_type, target, is_active)
Лог:    log.logs

Запуск:
    python parser.py
    python parser.py --headless
    python parser.py --retries 10 --debug
"""

import shutil
import os
import argparse
import base64
import csv
import io
import logging
import sys
import time
from pathlib import Path
from typing import Optional
from datetime import datetime

# UTF-8 вывод на Windows-консоли
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ddddocr
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service

TARGET_URL = "https://lk.rosreestr.ru/eservices/real-estate-objects-online"

INPUT_CSV  = Path("input.csv")
OUTPUT_CSV = Path("output.csv")
LOG_FILE   = Path("log.logs")

OUTPUT_FIELDNAMES = ["kadastral_number", "object_type", "target", "is_active"]

_SUBMIT_ID = "realestateobjects-search"

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
_OBJECT_TYPE_XPATHS_FALLBACK = [
    "//td[contains(., 'Вид объекта')]/following-sibling::td[1]",
    "//th[contains(., 'Вид объекта')]/following-sibling::td[1]",
    "//dt[contains(., 'Вид объекта')]/following-sibling::dd[1]",
    # Широкий //*[contains(text(),...)] убран: он ловил dropdown-фильтр формы
    # «Вид объекта» с placeholder «Выберите значение из справочника»
]

# Placeholder незаполненного dropdown-фильтра формы — не является результатом
_FORM_PLACEHOLDER = "выберите значение из справочника"

_JS_SET_VALUE = """
var el = arguments[0], val = arguments[1];
var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
setter.call(el, val);
el.dispatchEvent(new Event('input',  { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
"""


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("rosreestr")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def _read_input() -> list[str]:
    """Читает кадастровые номера из input.csv."""
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"Файл {INPUT_CSV} не найден.")

    numbers: list[str] = []
    with INPUT_CSV.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Ищем колонку kadastral_number, иначе берём первую
        col = None
        if reader.fieldnames:
            for name in reader.fieldnames:
                if name.strip().lower() in ("kadastral_number", "кадастровый номер", "kn"):
                    col = name
                    break
            if col is None:
                col = reader.fieldnames[0]
        for row in reader:
            val = row.get(col, "").strip()
            if val:
                numbers.append(val)
    return numbers


def _append_output(row: dict) -> None:
    """Дописывает одну строку в output.csv (создаёт файл с заголовком при необходимости)."""
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES, delimiter=';')
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------

def _find_edge_driver() -> str:
    """
    Ищет msedgedriver.exe: сначала рядом с parser.py, затем в PATH.
    Если не найден — выводит инструкцию по скачиванию и завершает работу.
    """
    local = Path(__file__).parent / "msedgedriver.exe"
    if local.exists():
        return str(local)

    in_path = shutil.which("msedgedriver") or shutil.which("msedgedriver.exe")
    if in_path:
        return in_path

    edge_exe = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    version_hint = ""
    if edge_exe.exists():
        app_dir = edge_exe.parent
        versions = sorted(app_dir.glob("[0-9]*"), reverse=True)
        if versions:
            version_hint = f"  Версия Edge: {versions[0].name}"

    raise RuntimeError(
        "msedgedriver.exe не найден.\n"
        "Скачайте по ссылке:\n"
        "  https://developer.microsoft.com/ru-ru/microsoft-edge/tools/webdriver/\n"
        f"{version_hint}\n"
        "Распакуйте msedgedriver.exe и положите рядом с parser.py"
    )


def _build_driver(headless: bool) -> webdriver.Edge:
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

    service = Service(executable_path=_find_edge_driver())
    driver = webdriver.Edge(service=service, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


# ---------------------------------------------------------------------------
# Элементы
# ---------------------------------------------------------------------------

def _find(driver: webdriver.Edge, selectors: list[str]) -> Optional[webdriver.remote.webelement.WebElement]:
    for sel in selectors:
        try:
            return driver.find_element(By.CSS_SELECTOR, sel)
        except NoSuchElementException:
            pass
    return None


def _wait_for(driver: webdriver.Edge, selectors: list[str], timeout: float = 15) -> Optional[webdriver.remote.webelement.WebElement]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        el = _find(driver, selectors)
        if el is not None:
            return el
        time.sleep(0.5)
    return None


def _react_fill(driver: webdriver.Edge, element: webdriver.remote.webelement.WebElement, value: str) -> None:
    element.click()
    element.clear()
    driver.execute_script(_JS_SET_VALUE, element, value)
    time.sleep(0.2)


# ---------------------------------------------------------------------------
# Капча
# ---------------------------------------------------------------------------

def _get_captcha_image(driver: webdriver.Edge) -> Image.Image:
    img_el = _find(driver, _CAPTCHA_IMG_FALLBACK)
    if img_el is None:
        raise RuntimeError("Изображение капчи не найдено")

    src: str = img_el.get_attribute("src") or ""
    if src.startswith("data:image"):
        raw = base64.b64decode(src.split(",", 1)[1])
        return Image.open(io.BytesIO(raw))

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

def _captcha_error(driver: webdriver.Edge) -> bool:
    text = driver.find_element(By.TAG_NAME, "body").text.lower()
    return any(k in text for k in ("неверн", "captcha", "повторите", "ошибка ввода"))


def _reload_captcha(driver: webdriver.Edge, timeout: float = 8) -> None:
    """
    Обновляет капчу и ждёт, пока src изображения реально сменится.
    Если кнопки reload нет — перезагружает страницу целиком.
    """
    # Запоминаем текущий src, чтобы детектировать смену
    old_src = ""
    img_el = _find(driver, _CAPTCHA_IMG_FALLBACK)
    if img_el:
        old_src = img_el.get_attribute("src") or ""

    clicked_reload = False
    try:
        reload_btn = driver.find_element(
            By.CSS_SELECTOR,
            ".rros-ui-lib-captcha-content-reload-btn, [class*='reload-btn']",
        )
        reload_btn.click()
        clicked_reload = True
    except NoSuchElementException:
        pass

    if not clicked_reload:
        # Кнопки нет — перезагружаем страницу целиком
        driver.get(TARGET_URL)
        time.sleep(3)
        return

    # Ждём, пока src изображения капчи изменится
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        img_el = _find(driver, _CAPTCHA_IMG_FALLBACK)
        if img_el:
            new_src = img_el.get_attribute("src") or ""
            if new_src and new_src != old_src:
                return  # новая капча загружена

    # Если src так и не сменился — перезагружаем страницу как крайний случай
    driver.get(TARGET_URL)
    time.sleep(3)


def _object_not_found(driver: webdriver.Edge) -> bool:
    text = driver.find_element(By.TAG_NAME, "body").text.lower()
    return any(k in text for k in ("не найден", "не обнаружен", "отсутствует", "not found"))


def _click_first_result(driver: webdriver.Edge) -> bool:
    """Кликает первую ссылку в таблице результатов. Возвращает True при успехе."""
    for sel in [
        "div[data-test-id^='cell-'] a",
        "table tbody tr:first-child a",
    ]:
        try:
            driver.find_element(By.CSS_SELECTOR, sel).click()
            return True
        except NoSuchElementException:
            pass
    return False


def _extract_detail_field(driver: webdriver.Edge, label: str) -> Optional[str]:
    """Извлекает значение поля по тексту метки на карточке объекта."""
    xpaths = [
        f"//*[normalize-space(.)='{label}']/following-sibling::*[normalize-space()][1]",
        f"//*[normalize-space(text())='{label}']/following-sibling::*[1]",
        f"//td[normalize-space(.)='{label}']/following-sibling::td[1]",
        f"//dt[normalize-space(.)='{label}']/following-sibling::dd[1]",
    ]
    for xpath in xpaths:
        try:
            el = driver.find_element(By.XPATH, xpath)
            val = el.text.strip()
            if val and val.lower() != _FORM_PLACEHOLDER:
                return val
        except NoSuchElementException:
            pass
    return None


# ---------------------------------------------------------------------------
# Результат
# ---------------------------------------------------------------------------

def _extract_object_type(driver: webdriver.Edge) -> Optional[str]:
    def _valid(val: str) -> bool:
        return bool(val) and val.lower() != _FORM_PLACEHOLDER

    # React-таблица: ищем колонку «Вид объекта» по data-test-id заголовка,
    # затем берём текст ячейки напрямую (без требования <a>-тега)
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
                By.XPATH, f"//div[@data-test-id='cell-{col_idx}']"
            )
            val = cell.text.strip()
            if _valid(val):
                return val
    except Exception:
        pass

    # Fallback для классических HTML-таблиц
    for xpath in _OBJECT_TYPE_XPATHS_FALLBACK:
        try:
            el = driver.find_element(By.XPATH, xpath)
            val = el.text.strip()
            if _valid(val):
                return val
        except NoSuchElementException:
            pass

    return None


# ---------------------------------------------------------------------------
# Отладка
# ---------------------------------------------------------------------------

def _save_debug(driver: webdriver.Edge, tag: str) -> None:
    driver.save_screenshot(f"debug_{tag}.png")
    Path(f"debug_{tag}.html").write_text(driver.page_source, encoding="utf-8")
    print(f"[debug] debug_{tag}.png  debug_{tag}.html")


# ---------------------------------------------------------------------------
# Парсинг одного КН (переиспользует открытый браузер)
# ---------------------------------------------------------------------------

def _parse_with_driver(
    driver: webdriver.Edge,
    cadastral_number: str,
    max_retries: int = 7,
    debug: bool = False,
) -> Optional[str]:
    """
    Возвращает «Вид объекта недвижимости» или None если объект не найден.

    Raises:
        RuntimeError: при критических ошибках или исчерпании попыток.
    """
    driver.get(TARGET_URL)
    time.sleep(3)

    if debug:
        _save_debug(driver, f"{cadastral_number.replace(':', '_')}_initial")

    for attempt in range(1, max_retries + 1):
        print(f"  [{attempt}/{max_retries}] Заполняем форму...")

        kn = _wait_for(driver, _KN_FALLBACK, timeout=15)
        if kn is None:
            if debug:
                _save_debug(driver, f"{cadastral_number.replace(':', '_')}_no_kn")
            raise RuntimeError("Поле кадастрового номера не найдено.")
        _react_fill(driver, kn, cadastral_number)

        try:
            cap_img = _get_captcha_image(driver)
            if debug:
                cap_img.save(f"debug_{cadastral_number.replace(':', '_')}_captcha_{attempt}.png")
            cap_text = _solve_captcha(cap_img)
            print(f"     Капча -> '{cap_text}'")
        except Exception as exc:
            print(f"     Ошибка капчи: {exc}")
            time.sleep(1)
            continue

        cap_in = _find(driver, _CAPTCHA_INPUT_FALLBACK)
        if cap_in is None:
            print("     Поле ввода капчи не найдено.")
            if debug:
                _save_debug(driver, f"{cadastral_number.replace(':', '_')}_no_cap_in_{attempt}")
            time.sleep(1)
            continue
        _react_fill(driver, cap_in, cap_text)

        # Inline-проверка сразу после ввода: клиентская валидация показывает
        # «Текст введен неверно» и блокирует кнопку ещё до сабмита.
        time.sleep(0.8)
        if _captcha_error(driver):
            print("     Неверная капча (inline), обновляем и повторяем...")
            _reload_captcha(driver)
            continue

        # Ждём активации кнопки; в каждой итерации также проверяем ошибку капчи
        submit = None
        deadline = time.time() + 5
        while time.time() < deadline:
            if _captcha_error(driver):
                break
            try:
                btn = driver.find_element(By.ID, _SUBMIT_ID)
                if not btn.get_attribute("disabled"):
                    submit = btn
                    break
            except NoSuchElementException:
                pass
            time.sleep(0.3)

        # Ошибка капчи обнаружена во время ожидания кнопки
        if submit is None and _captcha_error(driver):
            print("     Неверная капча (кнопка не активна), обновляем и повторяем...")
            _reload_captcha(driver)
            continue

        if submit is None:
            print("     Кнопка disabled, кликаем через JS...")
            try:
                btn = driver.find_element(By.ID, _SUBMIT_ID)
                driver.execute_script("arguments[0].click()", btn)
            except NoSuchElementException:
                raise RuntimeError("Кнопка 'Найти' не найдена на странице.")
        else:
            submit.click()

        time.sleep(3)

        # Серверная проверка — на случай если клиентская валидация не сработала
        if _captcha_error(driver):
            print("     Неверная капча (server), обновляем и повторяем...")
            _reload_captcha(driver)
            continue

        if _object_not_found(driver):
            return None

        result = _extract_object_type(driver)
        if result:
            # Переходим в карточку объекта для извлечения «Назначения»
            target = ""
            if _click_first_result(driver):
                time.sleep(2)
                target = _extract_detail_field(driver, "Назначение") or ""
            if debug:
                _save_debug(driver, f"{cadastral_number.replace(':', '_')}_success")
            return {"object_type": result, "target": target}

        if debug:
            _save_debug(driver, f"{cadastral_number.replace(':', '_')}_no_result_{attempt}")
            body = driver.find_element(By.TAG_NAME, "body").text
            print("[debug] Текст страницы (3000 симв.):")
            print(body[:3000])
        else:
            driver.save_screenshot(f"debug_result_{attempt}.png")
            print(f"     'Вид объекта' не найден. Скриншот: debug_result_{attempt}.png")

    raise RuntimeError(f"Не удалось получить данные за {max_retries} попыток.")


# ---------------------------------------------------------------------------
# Публичные функции
# ---------------------------------------------------------------------------

def parse(
    cadastral_number: str,
    headless: bool = False,
    max_retries: int = 7,
    debug: bool = False,
) -> Optional[dict]:
    """Парсит один КН, открывая и закрывая браузер.
    Возвращает {"object_type": str, "target": str} или None если не найден."""
    driver = _build_driver(headless)
    try:
        return _parse_with_driver(driver, cadastral_number, max_retries, debug)
    finally:
        driver.quit()


def run_batch(
    headless: bool = False,
    max_retries: int = 7,
    debug: bool = False,
) -> None:
    """
    Читает КН из input.csv, парсит каждый и пишет результаты в output.csv.
    Один браузер на весь батч.
    """
    logger = _setup_logger()

    numbers = _read_input()
    total = len(numbers)
    logger.info(f"Начало обработки. Всего номеров: {total}")

    driver = _build_driver(headless)
    try:
        for idx, kn in enumerate(numbers, 1):
            print(f"\n[{idx}/{total}] {kn}")
            try:
                res = _parse_with_driver(driver, kn, max_retries, debug)
                if res:
                    obj_type = res["object_type"]
                    target   = res.get("target", "")
                    row = {"kadastral_number": kn, "object_type": obj_type, "target": target, "is_active": 1}
                    _append_output(row)
                    logger.info(f"СОХРАНЕНО | {kn} | object_type={obj_type!r} | target={target!r} | is_active=1")
                else:
                    row = {"kadastral_number": kn, "object_type": "", "target": "", "is_active": 0}
                    _append_output(row)
                    logger.info(f"СОХРАНЕНО | {kn} | object_type='' | target='' | is_active=0 (объект не найден)")

            except RuntimeError as exc:
                row = {"kadastral_number": kn, "object_type": "", "target": "", "is_active": -1}
                _append_output(row)
                logger.error(f"ОШИБКА    | {kn} | {exc} | сохранено is_active=-1")

    finally:
        driver.quit()

    logger.info(f"Обработка завершена. Результаты: {OUTPUT_CSV}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Парсер Росреестра: читает кадастровые номера из input.csv, "
            "пишет результаты в output.csv"
        )
    )
    ap.add_argument("--headless", action="store_true", help="Браузер без окна")
    ap.add_argument("--retries", type=int, default=7, help="Попыток на один номер (по умолчанию 7)")
    ap.add_argument("--debug", action="store_true",
                    help="Сохранять скриншоты/HTML и выводить диагностику")
    args = ap.parse_args()

    print(f"Вход: {INPUT_CSV}  |  Выход: {OUTPUT_CSV}  |  Лог: {LOG_FILE}")
    with open(OUTPUT_CSV, 'w') as f:
        pass
    print(f"Режим: {'headless' if args.headless else 'с браузером'}, попыток: {args.retries}")
    print("=" * 60)

    try:
        run_batch(headless=args.headless, max_retries=args.retries, debug=args.debug)
    except KeyboardInterrupt as ex:
        print("\nПрервано пользователем.")
    finally:
        dt_prefix = datetime.now().strftime("%Y-%m-%d_%H %M %S")
        
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        archive_name = f"{OUTPUT_CSV.stem}.{dt_prefix}{OUTPUT_CSV.suffix}"
        copy_path = output_dir / archive_name
        shutil.copy(OUTPUT_CSV, copy_path)



if __name__ == "__main__":
    main()
