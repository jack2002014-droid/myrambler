import re
import os
import sys
import webbrowser
import pyperclip
from datetime import datetime, timedelta, timezone
from imap_tools import MailBox, A
from html.parser import HTMLParser

os.chdir(os.path.dirname(sys.executable))

auto_mode = False


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def get_text(self):
        return ' '.join(self.text)


def html_to_text(html):
    extractor = HTMLTextExtractor()
    extractor.feed(html)
    return extractor.get_text()


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def get_accounts():
    with open('creditials.txt', encoding='utf-8') as file:
        accounts = [line.strip() for line in file if line.strip()]
        return [a.split(':', 1) for a in accounts]


def extract_code(text, html=''):
    source = text or html_to_text(html or '')
    patterns = [
        r'(?:код|code|otp|пароль|password)[^\d]*(\d{4,8})',
        r'(\d{4,8})(?:[^\d]*(?:код|code|otp))',
        r'\b(\d{4,8})\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def actions(account_login, account_password):
    global auto_mode
    clear()
    try:
        mailbox = MailBox('imap.rambler.ru').login(account_login, account_password)
    except Exception as e:
        print(f'Не удалось войти в аккаунт {account_login}: {e}\n\nEnter, чтобы вернуться')
        input()
        return

    date_from = (datetime.now(timezone.utc) - timedelta(hours=24)).date()
    messages = []
    for msg in mailbox.fetch(A(date_gte=date_from)):
        messages.append((msg.uid, msg.subject, msg.date, msg.text, msg.html))
    messages.reverse()

    # Авто-режим — сразу берём последнее письмо и возвращаемся
    if auto_mode:
        clear()
        if not messages:
            print(f'[{account_login}] Нет писем за последние 24 часа')
            input('\nEnter, чтобы вернуться...')
            return
        uid, subject, date, text, html = messages[0]
        print(f'[АВТО] {account_login}\nТема: {subject}\n')
        code = extract_code(text or '', html or '')
        if code:
            pyperclip.copy(code)
            print(f'  Код: {code}')
            print(f'  ✓ Скопирован в буфер обмена')
        else:
            print('  Код не найден')
        return

    # Ручной режим
    while True:
        clear()
        if not messages:
            print(f'[{account_login}]\n\nНет писем за последние 24 часа\n\n[0] Вернуться\n')
            if input('Выбор: ') == '0':
                return
            continue

        messages_text = '\n'.join(
            f'[{i + 1}] {messages[i][2].strftime("%H:%M") if messages[i][2] else "??:??"} | {messages[i][1]}'
            for i in range(len(messages))
        )
        print(f'[{account_login}]\n\n{messages_text}\n\n[A] Авто — последнее письмо\n[0] Вернуться\n')
        choice = input('Выбор: ').strip()

        if choice == '0':
            return

        if choice.lower() == 'a':
            idx = 0
        else:
            try:
                idx = int(choice) - 1
                if not (0 <= idx < len(messages)):
                    continue
            except ValueError:
                continue

        clear()
        uid, subject, date, text, html = messages[idx]
        print(f'Тема: {subject}\nВремя: {date.strftime("%H:%M") if date else "неизвестно"}\n')

        code = extract_code(text or '', html or '')
        if code:
            pyperclip.copy(code)
            print(f'  Код: {code}')
            print(f'  ✓ Скопирован в буфер обмена')
        else:
            print('  Код не найден автоматически, открываю браузер...')
            try:
                with open('message.html', 'w', encoding='utf-8') as file:
                    file.write(html or text or 'Нет содержимого')
                webbrowser.open(f'file:///{os.getcwd()}/message.html')
            except Exception as e:
                print(f'Ошибка: {e}')

        input('\nEnter, чтобы вернуться...')


def main():
    global auto_mode
    accounts = get_accounts()
    while True:
        clear()
        auto_status = 'ВКЛ ✓' if auto_mode else 'ВЫКЛ'
        accounts_text = '\n'.join(f'[{i + 1}] {accounts[i][0]}' for i in range(len(accounts)))
        print(f'[Выбор аккаунта] [Авто-режим: {auto_status}]\n\n{accounts_text}\n\n[A] Переключить авто-режим\n[0] Выйти\n')
        choice = input('Выбор: ').strip()

        if choice == '0':
            return

        if choice.lower() == 'a':
            auto_mode = not auto_mode
            continue

        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(accounts)):
                continue
        except ValueError:
            continue

        actions(*accounts[idx])


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'Ошибка: {e}')
        input()