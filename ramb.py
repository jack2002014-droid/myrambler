import re
import os
import sys
import json
import glob
import time
import ctypes
import webbrowser
import winsound
from datetime import datetime, timedelta, timezone
from imap_tools import MailBox, A
from html.parser import HTMLParser

try:
    import pyperclip
except ImportError:
    print('Ошибка: не установлена библиотека pyperclip')
    print('Установите: pip install -r requirements.txt')
    input('\nEnter, чтобы выйти...')
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

try:
    import colorama
    # native VT в консоли Windows: цвета + движения курсора (для мыши)
    # работают напрямую, без обертки stdout
    try:
        colorama.just_fix_windows_console()
    except Exception:
        colorama.init()
    C_GREEN = '\033[92m'
    C_YELLOW = '\033[93m'
    C_RED = '\033[91m'
    C_CYAN = '\033[96m'
    C_MAGENTA = '\033[95m'
    C_WHITE = '\033[97m'
    C_DIM = '\033[2m'
    C_BOLD = '\033[1m'
    C_RESET = '\033[0m'
except ImportError:
    C_GREEN = C_YELLOW = C_RED = C_CYAN = C_MAGENTA = C_WHITE = ''
    C_DIM = C_BOLD = C_RESET = ''

# ASCII-маркер: ✓ в части шрифтов консоли рисуется квадратиком
MARK_OK = '[+]'

if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))
else:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

SETTINGS_FILE = 'auto_mode.json'
HISTORY_FILE = 'history.json'
ACCOUNTS_PAGE_SIZE = 10
MESSAGES_PAGE_SIZE = 10
FETCH_LIMIT = 30  # сколько последних писем тянуть с сервера (быстрее, чем все за 24ч)

# Поддерживаем оба имени файла, чтобы не сломать старый сценарий.
# Приоритет новому правильному имени, fallback — старое с опечаткой.
ACCOUNT_FILES = ('credentials.txt', 'creditials.txt')


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            data = json.load(f)
            return (
                data.get('auto', False),
                data.get('sound', True),
                int(data.get('wait_interval', 5)),
            )
    except Exception:
        return False, True, 5


def save_settings(auto, sound, wait_interval=5):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'auto': auto, 'sound': sound, 'wait_interval': wait_interval}, f)


auto_mode, sound_enabled, wait_interval = load_settings()
last_copied = None  # (login, code) — что последним ушло в буфер, показываем в меню


def load_history():
    try:
        with open(HISTORY_FILE, encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data][:5]
    except Exception:
        pass
    return []


def save_history(history):
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history[:5], f)
    except Exception:
        pass


def push_history(login):
    hist = load_history()
    hist = [login] + [h for h in hist if h != login]
    save_history(hist[:3])  # держим только 3 последних — для пачки из 10 этого хватает


def beep():
    if sound_enabled:
        try:
            winsound.MessageBeep()
        except Exception:
            pass


def clean_old_message_files():
    for f in glob.glob('message_*.html'):
        try:
            os.remove(f)
        except Exception:
            pass


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def get_text(self):
        return ' '.join(self.text)


def html_to_text(html):
    try:
        extractor = HTMLTextExtractor()
        extractor.feed(html or '')
        return extractor.get_text()
    except Exception:
        return html or ''


def clear():
    os.system("cls" if os.name == "nt" else "clear")


# ---------- Мышь в консоли (выбор кликом ЛКМ) ----------

ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
REV = '\x1b[7m'  # инверсия фона для подсветки строки под курсором


def strip_ansi(s):
    return ANSI_RE.sub('', s or '')


def _con_handles():
    try:
        if os.name != 'nt':
            return None
        k = ctypes.windll.kernel32
        return k, k.GetStdHandle(-10), k.GetStdHandle(-11)
    except Exception:
        return None


def _cursor_row():
    """Текущая строка курсора в буфере консоли (0-based). None — не удалось."""
    try:
        h = _con_handles()
        if not h:
            return None
        k, _, h_out = h

        class COORD(ctypes.Structure):
            _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [('Left', ctypes.c_short), ('Top', ctypes.c_short),
                        ('Right', ctypes.c_short), ('Bottom', ctypes.c_short)]

        class CSBI(ctypes.Structure):
            _fields_ = [('Size', COORD), ('CursorPosition', COORD),
                        ('Attributes', ctypes.c_uint16), ('Window', SMALL_RECT),
                        ('MaxWindowSize', COORD)]

        csbi = CSBI()
        if k.GetConsoleScreenBufferInfo(h_out, ctypes.byref(csbi)):
            return int(csbi.CursorPosition.Y)
    except Exception:
        pass
    return None


def _mouse_enable():
    """Включает прием событий мыши. Возвращает старый режим консоли или None."""
    try:
        h = _con_handles()
        if not h:
            return None
        k, h_in, _ = h
        mode = ctypes.c_ulong(0)
        if not k.GetConsoleMode(h_in, ctypes.byref(mode)):
            return None
        old = mode.value
        new = (old | 0x10 | 0x80) & ~0x40  # MOUSE_INPUT + EXTENDED_FLAGS, без QUICK_EDIT
        if not k.SetConsoleMode(h_in, new):
            return None
        k.FlushConsoleInputBuffer(h_in)
        return old
    except Exception:
        return None


def _mouse_restore(old):
    try:
        if old is not None:
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-10), old)
    except Exception:
        pass


class _MCoord(ctypes.Structure):
    _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]


class _KeyEv(ctypes.Structure):
    _fields_ = [('bKeyDown', ctypes.c_int32),
                ('wRepeatCount', ctypes.c_uint16),
                ('wVirtualKeyCode', ctypes.c_uint16),
                ('wVirtualScanCode', ctypes.c_uint16),
                ('uChar', ctypes.c_uint16),
                ('dwControlKeyState', ctypes.c_uint32)]


class _MouseEv(ctypes.Structure):
    _fields_ = [('dwMousePosition', _MCoord),
                ('dwButtonState', ctypes.c_ulong),
                ('dwControlKeyState', ctypes.c_ulong),
                ('dwEventFlags', ctypes.c_ulong)]


class _EvUnion(ctypes.Union):
    _fields_ = [('KeyEvent', _KeyEv), ('MouseEvent', _MouseEv)]


class _InputRec(ctypes.Structure):
    _fields_ = [('EventType', ctypes.c_ushort), ('Event', _EvUnion)]


def menu_select(prompt, n_items, top_row, line_maker):
    """Ввод выбора в меню.

    prompt — приглашение, n_items — строк в списке, top_row — строка первой
    строки списка (0-based, из _cursor_row), line_maker(i, hl) — текст строки.
    Возвращает ('click', i) при клике ЛКМ по строке или ('key', текст) при Enter.
    Если мышь недоступна — обычный input(), поведение как раньше.
    """
    if n_items <= 0 or top_row is None or not sys.stdin.isatty():
        try:
            return ('key', input(prompt).strip())
        except EOFError:
            return ('key', '0')
    old = _mouse_enable()
    if old is None:
        try:
            return ('key', input(prompt).strip())
        except EOFError:
            return ('key', '0')
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        prompt_row = _cursor_row()
        if prompt_row is None:
            return ('key', input().strip())
        buf = []
        hl = None

        def paint(row, text):
            # row 0-based -> ANSI 1-based; вернуться на строку приглашения
            col = len(strip_ansi(prompt)) + len(''.join(buf)) + 1
            sys.stdout.write(f'\x1b[{row + 1};1H\x1b[K{text}\x1b[{prompt_row + 1};{col}H')
            sys.stdout.flush()

        def unhl():
            nonlocal hl
            if hl is not None:
                try:
                    paint(top_row + hl, line_maker(hl, False))
                except Exception:
                    pass
                hl = None

        def repaint_prompt():
            col = len(strip_ansi(prompt)) + 1
            sys.stdout.write(f'\x1b[{prompt_row + 1};1H\x1b[K{prompt}{"".join(buf)}')
            sys.stdout.write(f'\x1b[{prompt_row + 1};{col + len("".join(buf))}H')
            sys.stdout.flush()

        k = ctypes.windll.kernel32
        h_in = k.GetStdHandle(-10)
        while True:
            rec = _InputRec()
            n = ctypes.c_ulong(0)
            if not k.ReadConsoleInputW(h_in, ctypes.byref(rec), 1, ctypes.byref(n)):
                continue
            et = rec.EventType
            if et == 1:  # клавиатура — как обычный ввод + Enter
                ke = rec.Event.KeyEvent
                if not ke.bKeyDown:
                    continue
                vk = ke.wVirtualKeyCode
                if vk == 0x0D:  # Enter
                    unhl()
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    return ('key', ''.join(buf).strip())
                elif vk == 0x08:  # Backspace
                    if buf:
                        buf.pop()
                        repaint_prompt()
                elif vk == 0x1B:  # Esc — очистить набранное
                    if buf:
                        buf = []
                        repaint_prompt()
                else:
                    ch = chr(ke.uChar) if ke.uChar else ''
                    if ch and ch.isprintable():
                        buf.append(ch)
                        sys.stdout.write(ch)
                        sys.stdout.flush()
            elif et == 2:  # мышь
                me = rec.Event.MouseEvent
                y = int(me.dwMousePosition.Y)
                if me.dwEventFlags == 0 and (me.dwButtonState & 0x01):
                    # клик ЛКМ по строке списка
                    if top_row <= y < top_row + n_items:
                        unhl()
                        sys.stdout.write('\n')
                        sys.stdout.flush()
                        return ('click', y - top_row)
                elif me.dwEventFlags == 0x01:  # движение — подсветка
                    if top_row <= y < top_row + n_items:
                        i = y - top_row
                        if i != hl:
                            unhl()
                            hl = i
                            try:
                                paint(top_row + i, line_maker(i, True))
                            except Exception:
                                hl = None
                    else:
                        unhl()
    finally:
        _mouse_restore(old)


def accounts_file_in_use():
    for name in ACCOUNT_FILES:
        if os.path.exists(name):
            return name
    return ACCOUNT_FILES[0]


def get_accounts():
    fname = accounts_file_in_use()
    if not os.path.exists(fname):
        return []
    with open(fname, encoding='utf-8') as file:
        accounts = []
        for line in file:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' not in line:
                continue
            login, pwd = line.split(':', 1)
            login, pwd = login.strip(), pwd.strip()
            if login and pwd:
                accounts.append((login, pwd))
        return accounts


def short(s, n):
    s = (s or '').replace('\n', ' ').replace('\r', ' ').strip()
    return s if len(s) <= n else s[:n - 1] + '…'


# ---------- Коды ----------

KEYWORD_RE = re.compile(
    r'(?:код|kod|code|otp|one[\s_-]?time|verification|verify|confirm|подтвержд|пароль|password)[^\d]{0,40}(\d[\d\s\-]{3,11}\d)',
    re.IGNORECASE,
)

STANDALONE_RE = re.compile(r'(?<![\d+\-_.])(\d{4,8})(?![\d_.])')

FALSE_YEARS = set(str(y) for y in range(1930, 2036))


def _clean_candidate(raw):
    digits = re.sub(r'[\s\-]', '', raw or '')
    if not digits.isdigit():
        return None
    if not (4 <= len(digits) <= 8):
        return None
    if len(digits) == 4 and digits in FALSE_YEARS:
        return None  # год, а не код
    return digits


def extract_all_codes(text, html=''):
    """Возвращает все кандидаты по приоритету: сначала около слов код/code/otp, потом отдельно стоящие."""
    # как раньше: сначала обычный текст, html — только если текста нет (быстрее и без дублей)
    combined = (text or html_to_text(html or '') or '').strip()
    if not combined:
        return []
    keyword_hits = []
    standalone_hits = []
    seen = set()

    for m in KEYWORD_RE.finditer(combined):
        c = _clean_candidate(m.group(1))
        if c and c not in seen:
            seen.add(c)
            keyword_hits.append(c)

    for m in STANDALONE_RE.finditer(combined):
        c = _clean_candidate(m.group(1))
        if c and c not in seen:
            seen.add(c)
            standalone_hits.append(c)

    # внутри каждой группы 5-6 значные (самые вероятные OTP) — первыми, стабильно
    keyword_hits.sort(key=lambda c: (0 if len(c) in (5, 6) else 1))
    standalone_hits.sort(key=lambda c: (0 if len(c) in (5, 6) else 1))
    # кандидаты около слов код/code/otp важнее отдельно стоящих цифр
    return keyword_hits + standalone_hits


def extract_code(text, html=''):
    """Совместимая обертка: как раньше — один код или None."""
    codes = extract_all_codes(text, html)
    return codes[0] if codes else None


def compute_has_codes(messages):
    return [bool(extract_code(m['text'] or '', m['html'] or '')) for m in messages]


def format_message_line(disp_num, msg, has_code):
    time_str = msg['date'].strftime("%H:%M") if msg['date'] else "??:??"
    num = f'{C_BOLD}{C_YELLOW}[{disp_num}]{C_RESET}'
    if has_code:
        # строка с кодом — зеленая целиком, сразу видно
        return (f'{num} {C_GREEN}{MARK_OK}{C_RESET} '
                f'{C_GREEN}{time_str} | {short(msg["from"], 22):22} | {short(msg["subject"], 42)}{C_RESET}')
    return (f'{num}    {C_DIM}{time_str}{C_RESET} | '
            f'{C_CYAN}{short(msg["from"], 22):22}{C_RESET} | {short(msg["subject"], 42)}')


def highlight_line(plain):
    return f'{REV}{strip_ansi(plain)}{C_RESET}'


# ---------- IMAP ----------

def fetch_messages(mailbox, limit=FETCH_LIMIT):
    date_from = (datetime.now(timezone.utc) - timedelta(hours=24)).date()
    messages = []
    # reverse=True + limit: сервер отдает сначала новые, тянем только 30 последних — быстро
    for msg in mailbox.fetch(A(date_gte=date_from), limit=limit, reverse=True):
        messages.append({
            'uid': msg.uid,
            'subject': msg.subject or '(без темы)',
            'date': msg.date,
            'from': getattr(msg, 'from_', '') or '',
            'text': msg.text or '',
            'html': msg.html or '',
        })
    return messages


def copy_code(code):
    pyperclip.copy(code)
    beep()
    print(f'\n  {C_BOLD}{C_GREEN}Код: {code}{C_RESET}')
    print(f'  {C_GREEN}{MARK_OK} Скопирован в буфер обмена{C_RESET}')


def status_tag(on):
    return f'{C_GREEN}ВКЛ {MARK_OK}{C_RESET}' if on else f'{C_RED}ВЫКЛ{C_RESET}'


def show_code_with_fallback(account_login, msg):
    """Показывает код(ы). Возвращает скопированный код или None."""
    codes = extract_all_codes(msg['text'] or '', msg['html'] or '')
    print(f'{C_DIM}От:{C_RESET} {C_CYAN}{msg["from"]}{C_RESET}\n'
          f'{C_DIM}Тема:{C_RESET} {C_WHITE}{msg["subject"]}{C_RESET}\n'
          f'{C_DIM}Время:{C_RESET} {msg["date"].strftime("%d.%m %H:%M") if msg["date"] else "неизвестно"}\n')
    if codes:
        copy_code(codes[0])
        if len(codes) > 1:
            print(f'  {C_DIM}Другие варианты: {", ".join(codes[1:4])}{C_RESET}')
            print(f'  {C_DIM}[2-{len(codes)}] взять другой вариант{C_RESET}')
        return codes
    print(f'  {C_YELLOW}Код не найден автоматически, открываю браузер...{C_RESET}')
    try:
        filename = f'message_{msg["uid"]}.html'
        with open(filename, 'w', encoding='utf-8') as file:
            file.write(msg['html'] or msg['text'] or 'Нет содержимого')
        webbrowser.open(f'file:///{os.getcwd()}/{filename}')
    except Exception as e:
        print(f'{C_RED}Ошибка: {e}{C_RESET}')
    return []


def wait_for_new_code(mailbox, known_uids, account_login):
    """W-режим: ждет новое письмо и сразу копирует код. Ctrl+C — отмена."""
    print(f'\n{C_CYAN}Жду новое письмо для {account_login} (опрос каждые {wait_interval}с, Ctrl+C — отмена)...{C_RESET}')
    try:
        while True:
            time.sleep(wait_interval)
            fresh = fetch_messages(mailbox)
            new = [m for m in fresh if m['uid'] not in known_uids]
            if new:
                msg = new[0]
                print(f'\n{C_GREEN}Пришло: {msg["subject"]} от {msg["from"]}{C_RESET}\n')
                codes = show_code_with_fallback(account_login, msg)
                return new, codes
            print(f'{C_DIM}.{C_RESET}', end='', flush=True)
    except KeyboardInterrupt:
        print(f'\n{C_YELLOW}Ожидание отменено.{C_RESET}')
        return None, []


def actions(account_login, account_password, accounts=None, idx=None, start_mode='menu'):
    """Возвращает: 'back' | ('next', next_idx) | 'quit'.

    accounts/idx — для сценария пачки по 10: после кода жмешь N
    и сразу попадаешь в следующий ящик. start_mode='auto' (клик мышью
    по почте) — сразу взять код из последнего письма, как в авто-режиме.
    """
    global last_copied
    accounts = accounts or []
    clear()
    print(f'{C_CYAN}Подключаюсь к {account_login}...{C_RESET}')
    try:
        mailbox = MailBox('imap.rambler.ru').login(account_login, account_password)
    except Exception as e:
        print(f'{C_RED}Не удалось войти в аккаунт {account_login}: {e}{C_RESET}\n\nEnter, чтобы вернуться')
        input()
        return 'back'

    def next_idx():
        if accounts and idx is not None and 0 <= idx + 1 < len(accounts):
            return idx + 1
        return None

    def next_login():
        n = next_idx()
        return accounts[n][0] if n is not None else None

    try:
        print(f'{C_CYAN}Загружаю последние письма...{C_RESET}')
        messages = fetch_messages(mailbox)
        has_codes = compute_has_codes(messages)
        push_history(account_login)

        # Авто-режим (или клик мышью по почте): код в буфер — и сразу в меню
        if auto_mode or start_mode == 'auto':
            clear()
            if not messages:
                print(f'[{account_login}] Нет писем за последние 24 часа')
                ans = input('\n[W] ждать новое / Enter — назад: ').strip().lower()
                if ans == 'w':
                    known = set()
                    res, codes = wait_for_new_code(mailbox, known, account_login)
                    if res and codes:
                        last_copied = (account_login, codes[0])
                return 'back'
            msg = messages[0]
            print(f'[АВТО] {account_login}\n')
            codes = show_code_with_fallback(account_login, msg)
            if codes:
                last_copied = (account_login, codes[0])
            return 'back'

        # Ручной режим
        page = 0
        page_size = MESSAGES_PAGE_SIZE
        while True:
            clear()
            if not messages:
                print(f'[{account_login}]\n\nНет писем за последние 24 часа\n\n[W] Ждать новое\n[R] Обновить\n[0] Вернуться\n')
                choice = input('Выбор: ').strip().lower()
                if choice == '0':
                    return 'back'
                if choice == 'w':
                    res, _ = wait_for_new_code(mailbox, set(), account_login)
                    if res:
                        messages = res
                        has_codes = compute_has_codes(messages)
                    continue
                if choice == 'r':
                    print('Обновляю список писем...')
                    messages = fetch_messages(mailbox)
                    has_codes = compute_has_codes(messages)
                continue

            total_pages = (len(messages) + page_size - 1) // page_size
            # страницу не сбрасываем: остаемся где были, пока сам не перелистнешь
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            end = min(start + page_size, len(messages))
            count = end - start

            plain_lines = [
                format_message_line(k + 1, messages[start + k], has_codes[start + k])
                for k in range(count)
            ]

            nav = ''
            if page > 0:
                nav += ' [Z] Назад'
            if page < total_pages - 1:
                nav += ' [X] Вперёд'

            nxt = next_login()
            nxt_hint = f'\n{C_DIM}[Q]{C_RESET} След. аккаунт: {C_CYAN}{nxt}{C_RESET}' if nxt else ''
            print(f'{C_BOLD}{C_CYAN}[{account_login}]{C_RESET} '
                  f'{C_DIM}(стр. {page + 1}/{total_pages}, всего {len(messages)}){C_RESET}\n')
            list_top = _cursor_row()
            for ln in plain_lines:
                print(ln)
            print(f'{C_DIM}\n{nav}\n[A] Авто — последнее письмо\n[W] Ждать новое\n[R] Обновить{nxt_hint}\n[0] Вернуться\n[мышь] клик по письму — открыть и скопировать код{C_RESET}')
            res = menu_select(f'{C_BOLD}{C_GREEN}Выбор (1-{count} / команда): {C_RESET}',
                              count, list_top,
                              lambda i, hl: highlight_line(plain_lines[i]) if hl else plain_lines[i])

            if res[0] == 'click':
                idx_sel = start + res[1]
            else:
                choice = res[1]
                low = choice.lower()
                if choice == '0':
                    return 'back'
                if low == 'a':
                    idx_sel = 0
                elif low == 'r':
                    print('Обновляю список писем...')
                    messages = fetch_messages(mailbox)
                    has_codes = compute_has_codes(messages)
                    continue  # страница сохраняется (clamp выше), не сбрасывается на 1-ю
                elif low == 'w':
                    known = {m['uid'] for m in messages}
                    res_w, _ = wait_for_new_code(mailbox, known, account_login)
                    if res_w:
                        messages = res_w + messages
                        has_codes = compute_has_codes(messages)
                    continue  # остаемся на своей странице
                elif low == 'z' and page > 0:
                    page -= 1
                    continue
                elif low == 'x' and page < total_pages - 1:
                    page += 1
                    continue
                elif low == 'q' and nxt is not None:
                    return ('next', next_idx())
                else:
                    try:
                        d = int(choice)
                        if not (1 <= d <= count):
                            continue
                        idx_sel = start + d - 1
                    except ValueError:
                        continue

            clear()
            msg = messages[idx_sel]
            codes = show_code_with_fallback(account_login, msg)

            # выбор другого варианта кода, если первый ложный
            if len(codes) > 1:
                alt = input('\nВариант 1 уже в буфере. Номер другого (Enter — пропуск): ').strip()
                try:
                    ai = int(alt) - 1
                    if 0 <= ai < len(codes):
                        copy_code(codes[ai])
                except ValueError:
                    pass

            nxt2 = next_login()
            hint = f' / [N] следующий: {nxt2}' if nxt2 else ''
            ans = input(f'\nEnter — список писем{hint} / [0] меню: ').strip().lower()
            if ans == '0':
                return 'back'
            if ans == 'n' and nxt2 is not None:
                return ('next', next_idx())
            # иначе остаемся в списке писем
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


def main():
    global auto_mode, sound_enabled
    clean_old_message_files()
    accounts = get_accounts()
    if len(accounts) == 1:
        # один аккаунт — сразу в него, дальше обычное меню (без зацикливания)
        actions(accounts[0][0], accounts[0][1], accounts, 0)
        accounts = get_accounts()

    acc_page = 0
    query = ''
    last_idx = None
    while True:
        clear()
        if not accounts:
            print(f'{C_YELLOW}Нет аккаунтов. Добавьте их в {accounts_file_in_use()} в формате email:password{C_RESET}\n')
            input('Enter, чтобы выйти...')
            return
        auto_status = status_tag(auto_mode)
        sound_status = status_tag(sound_enabled)

        # фильтр поиском: вводишь часть почты вместо номера 14/15/16
        if query:
            view = [(i, a) for i, a in enumerate(accounts) if query in a[0].lower()]
        else:
            view = list(enumerate(accounts))

        # последние использованные сверху (подсказка для пачки)
        hist = [h for h in load_history() if any(a[0] == h for a in accounts)]
        hist_text = ''
        if hist:
            hist_text = (f'{C_DIM}Последние: {C_RESET}'
                         + ', '.join(f'{C_CYAN}{h}{C_RESET}' for h in hist) + '\n\n')

        if not view:
            print(f'{C_YELLOW}[Выбор аккаунта] Поиск: "{query}" — ничего не найдено{C_RESET}\n\n[/] Новый поиск\n[C] Очистить\n[0] Выйти\n')
            choice = input('Выбор: ').strip().lower()
            if choice == 'c':
                query = ''
            elif choice == '0':
                return
            elif choice.startswith('/'):
                query = choice[1:].strip().lower()
            else:
                query = ''
            continue

        total_pages = (len(view) + ACCOUNTS_PAGE_SIZE - 1) // ACCOUNTS_PAGE_SIZE
        acc_page = max(0, min(acc_page, total_pages - 1))
        start = acc_page * ACCOUNTS_PAGE_SIZE
        end = min(start + ACCOUNTS_PAGE_SIZE, len(view))
        page_items = view[start:end]  # [(orig_idx, (login, pwd)), ...]
        count = len(page_items)
        # нумерация с 1 на каждой странице
        plain_lines = []
        for k, (orig_i, (login, _)) in enumerate(page_items):
            cur = f' {C_GREEN}→{C_RESET}' if orig_i == last_idx else ''
            plain_lines.append(f'{C_BOLD}{C_YELLOW}[{k + 1}]{C_RESET} {C_WHITE}{login}{C_RESET}{cur}')

        nav = ''
        if acc_page > 0:
            nav += ' [Z] Назад'
        if acc_page < total_pages - 1:
            nav += ' [X] Вперёд'
        q_hint = f' {C_MAGENTA}[Поиск: "{query}" / C-очистить]{C_RESET}' if query else ''
        buf_text = ''
        if last_copied:
            buf_text = (f'{C_DIM}В буфере: {C_RESET}{C_BOLD}{C_GREEN}{last_copied[1]}{C_RESET}'
                        f'{C_DIM} ({last_copied[0]}){C_RESET}\n')

        print(f'{C_BOLD}{C_CYAN}[Выбор аккаунта]{C_RESET} [Авто: {auto_status}] [Звук: {sound_status}]{q_hint}\n'
              f'{C_DIM}Аккаунтов: {len(accounts)} (стр. {acc_page + 1}/{total_pages}){C_RESET}\n'
              f'{buf_text}\n{hist_text}', end='')
        list_top = _cursor_row()
        for ln in plain_lines:
            print(ln)
        print(f'{C_DIM}\n{nav}\n[/текст] Поиск по почте\n[A] Авто-режим\n[S] Звук вкл/выкл\n[0] Выйти\n[мышь] клик по почте — зайти и скопировать код{C_RESET}')
        res = menu_select(f'{C_BOLD}{C_GREEN}Выбор (1-{count} / команда): {C_RESET}',
                          count, list_top,
                          lambda i, hl: highlight_line(plain_lines[i]) if hl else plain_lines[i])

        if res[0] == 'click':
            last_idx = page_items[res[1]][0]
            res = actions(*accounts[last_idx], accounts, last_idx, start_mode='auto')
        else:
            choice = res[1]
            if choice == '0':
                return
            low = choice.lower()
            if low == 'a':
                auto_mode = not auto_mode
                save_settings(auto_mode, sound_enabled, wait_interval)
                continue
            if low == 's':
                sound_enabled = not sound_enabled
                save_settings(auto_mode, sound_enabled, wait_interval)
                continue
            if low == 'c' and query:
                query = ''
                acc_page = 0
                continue
            if low == 'z' and acc_page > 0:
                acc_page -= 1
                continue
            if low == 'x' and acc_page < total_pages - 1:
                acc_page += 1
                continue
            if choice.startswith('/'):
                query = choice[1:].strip().lower()
                acc_page = 0
                continue
            # удобство: если введен текст без / и это не команда — считаем поиском
            if not choice.isdigit():
                # может это часть почты (вместо номера)
                q = choice.lower()
                if not q:
                    continue  # пустой Enter — ничего не делать, а не открывать совпадение
                matches = [i for i, a in enumerate(accounts) if q in a[0].lower()]
                if len(matches) == 1:
                    last_idx = matches[0]
                    res = actions(*accounts[matches[0]], accounts, matches[0])
                elif matches:
                    query = q
                    acc_page = 0
                    continue
                else:
                    continue
            else:
                try:
                    d = int(choice)
                    if not (1 <= d <= count):
                        continue
                    last_idx = page_items[d - 1][0]
                    res = actions(*accounts[last_idx], accounts, last_idx)
                except ValueError:
                    continue

        # обработка возврата из ящика: цепочка N для пачки по 10
        while isinstance(res, tuple) and res[0] == 'next':
            nxt = res[1]
            last_idx = nxt
            # держим страницу аккаунтов на нужном месте
            if not query:
                acc_page = nxt // ACCOUNTS_PAGE_SIZE
            res = actions(*accounts[nxt], accounts, nxt)
        if res == 'quit':
            return
        # 'back' — просто обновить список и показать меню (аккаунты могли поменяться)
        accounts = get_accounts()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'{C_RED}Ошибка: {e}{C_RESET}')
        input()
