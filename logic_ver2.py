import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

API_BASE = "https://api.remanga.org/api/v2"
SETTINGS_FILE = Path(__file__).with_name("settings.txt")


class DecksLogicError(Exception):
    """Ошибка логики/API, которую можно показать в GUI."""


@dataclass
class RunConfig:
    token: str
    user_id: int
    collection: Dict[str, Any]
    target_deck_id: int
    open_count: int = 0
    same_priority_rule: int = 1
    all_owned_rule: int = 1
    no_priority_rule: int = 1
    priority_owned_rule: int = 1
    is_premium: bool = False


class StopReason(Exception):
    """Остановка открытия по пользовательскому правилу."""


def create_settings_template() -> None:
    SETTINGS_FILE.write_text(
        "YOUR_REMANGA_TOKEN\n"
        "YOUR_REMANGA_ID\n",
        encoding="utf-8",
    )


def read_settings() -> Tuple[str, int]:
    """
    settings.txt поддерживает два простых варианта:

    1) Две строки:
       токен
       айди

    2) Или key=value:
       token=...
       user_id=...
    """
    if not SETTINGS_FILE.exists():
        create_settings_template()
        print(f"Создан файл настроек: {SETTINGS_FILE}")
        print("Заполните в нём токен и айди пользователя, затем запустите программу снова.")
        sys.exit(0)

    raw_lines = [line.strip() for line in SETTINGS_FILE.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in raw_lines if line and not line.startswith("#")]

    values: Dict[str, str] = {}
    positional: List[str] = []

    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
        else:
            positional.append(line)

    token = values.get("token") or values.get("токен") or (positional[0] if len(positional) >= 1 else "")
    user_id_text = values.get("user_id") or values.get("userid") or values.get("айди") or values.get("id") or (
        positional[1] if len(positional) >= 2 else ""
    )

    if not token or token == "YOUR_REMANGA_TOKEN":
        print(f"Ошибка: укажите токен в {SETTINGS_FILE}")
        sys.exit(1)

    try:
        user_id = int(user_id_text)
    except ValueError:
        print(f"Ошибка: во второй строке {SETTINGS_FILE} должен быть числовой айди пользователя.")
        sys.exit(1)

    return token, user_id


def make_headers(token: str, content_type: bool = False) -> Dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,ru;q=0.8",
        "authorization": f"Bearer {token}",
        "origin": "https://remanga.org",
        "referer": "https://remanga.org/",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    }
    if content_type:
        headers["content-type"] = "application/json"
    return headers


def request_or_stop(response: requests.Response, action: str) -> Any:
    if response.status_code >= 400:
        raise DecksLogicError(
            f"Ошибка: {action}\n"
            f"HTTP {response.status_code}\n"
            f"{response.text}"
        )

    if not response.text:
        return None

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise DecksLogicError(
            f"Ошибка: сервер вернул не JSON при действии: {action}\n"
            f"{response.text}"
        ) from exc


def ask_int(prompt: str, allowed: Optional[Set[int]] = None, min_value: Optional[int] = None) -> int:
    while True:
        text = input(prompt).strip()
        try:
            value = int(text)
        except ValueError:
            print("Введи число.")
            continue

        if allowed is not None and value not in allowed:
            print(f"Допустимые значения: {', '.join(map(str, sorted(allowed)))}")
            continue

        if min_value is not None and value < min_value:
            print(f"Минимальное значение: {min_value}")
            continue

        return value


def get_rare_collections(session: requests.Session, token: str, user_id: int) -> List[Dict[str, Any]]:
    page = 1
    collections: List[Dict[str, Any]] = []

    while True:
        url = f"{API_BASE}/inventory/{user_id}/rare-collections/"
        params = {"count": 20, "page": page, "ordering": "-percent"}
        response = session.get(url, headers=make_headers(token), params=params)
        data = request_or_stop(response, f"получение коллекций page={page}")
        collections.extend(data.get("results", []))

        if not data.get("next"):
            break
        page += 1

    return collections


def select_collection(session: requests.Session, token: str, user_id: int) -> Dict[str, Any]:
    collections = get_rare_collections(session, token, user_id)

    if not collections:
        print("Коллекции не найдены.")
        sys.exit(0)

    print("\nКакую коллекцию собираешь?")
    for index, collection in enumerate(collections, start=1):
        name = collection.get("name") or f"Коллекция id={collection.get('id')}"
        percent = collection.get("percent")
        suffix = f" — {percent}%" if percent is not None else ""
        print(f"{index}) {name}{suffix}")

    choice = ask_int("Номер коллекции: ", allowed=set(range(1, len(collections) + 1)))
    return collections[choice - 1]


def get_collection_card_ids(collection: Dict[str, Any]) -> Set[int]:
    cards = collection.get("cards", []) or []
    return {int(card["id"]) for card in cards if card.get("id") is not None}


def get_missing_collection_ids(collection: Dict[str, Any]) -> Set[int]:
    cards = collection.get("cards", []) or []
    return {int(card["id"]) for card in cards if card.get("id") is not None and not card.get("has", False)}


def get_owned_ids(session: requests.Session, token: str, ids: Set[int]) -> Set[int]:
    if not ids:
        return set()

    url = f"{API_BASE}/inventory/cards/has_cards/"
    owned: Set[int] = set()
    sorted_ids = sorted(ids)

    # На случай больших коллекций делаем чанками, чтобы URL не стал слишком длинным.
    for start in range(0, len(sorted_ids), 250):
        chunk = sorted_ids[start:start + 250]
        params = [("card_id", card_id) for card_id in chunk]
        response = session.get(url, headers=make_headers(token), params=params)
        data = request_or_stop(response, "получение имеющихся карт")
        owned.update(item["card_id"] for item in data if item.get("stack_count", 0) > 0)

    return owned


def get_inventory_decks(session: requests.Session, token: str, user_id: int, target_deck_id: int) -> List[int]:
    page = 1
    inventory_ids: List[int] = []

    while True:
        url = f"{API_BASE}/inventory/decks/"
        params = {"is_opened": "false", "user_id": user_id, "page": page}
        response = session.get(url, headers=make_headers(token), params=params)
        data = request_or_stop(response, f"получение доступных колод page={page}")

        for item in data.get("results", []):
            if item.get("deck", {}).get("id") == target_deck_id:
                inventory_ids.append(item["id"])

        if not data.get("next"):
            break
        page += 1

    return inventory_ids



def get_available_deck_types(session: requests.Session, token: str, user_id: int) -> List[Dict[str, Any]]:
    """
    Возвращает уникальные типы доступных неоткрытых колод.

    API возвращает отдельные inventory-записи, поэтому одинаковые deck.id
    группируются в одну запись с count и inventory_ids.
    """
    page = 1
    grouped: Dict[int, Dict[str, Any]] = {}

    while True:
        url = f"{API_BASE}/inventory/decks/"
        params = {"is_opened": "false", "user_id": user_id, "page": page}
        response = session.get(url, headers=make_headers(token), params=params)
        data = request_or_stop(response, f"получение доступных колод page={page}")

        for item in data.get("results", []):
            deck = item.get("deck") or {}
            deck_id = deck.get("id")
            if deck_id is None:
                continue

            deck_id = int(deck_id)

            if deck_id not in grouped:
                grouped[deck_id] = {
                    "id": deck_id,
                    "name": deck.get("name") or f"Колода id={deck_id}",
                    "deck": deck,
                    "count": 0,
                    "inventory_ids": [],
                }

            grouped[deck_id]["count"] += 1
            if item.get("id") is not None:
                grouped[deck_id]["inventory_ids"].append(int(item["id"]))

        if not data.get("next"):
            break
        page += 1

    return sorted(
        grouped.values(),
        key=lambda item: (str(item.get("name", "")).lower(), int(item.get("id", 0))),
    )


def open_deck(session: requests.Session, token: str, inventory_id: int) -> List[Dict[str, Any]]:
    url = f"{API_BASE}/inventory/decks/{inventory_id}/open/"
    response = session.post(url, headers=make_headers(token), data=b"")
    return request_or_stop(response, f"открытие колоды inventory_id={inventory_id}")


def choose_card(session: requests.Session, token: str, inventory_id: int, card_id: int) -> Any:
    url = f"{API_BASE}/inventory/decks/{inventory_id}/choose/"
    response = session.post(
        url,
        headers=make_headers(token, content_type=True),
        json={"card_id": card_id},
    )
    return request_or_stop(response, f"выбор карты card_id={card_id}")


def print_opened_cards(opened_cards: List[Dict[str, Any]], priority_ids: Set[int], owned_ids: Set[int]) -> None:
    print("Выпали карты:")
    for card in opened_cards:
        card_id = int(card["id"])
        marks = []
        if card_id in priority_ids:
            marks.append("ПРИОРИТЕТ")
        if card_id in owned_ids:
            marks.append("УЖЕ ЕСТЬ")
        else:
            marks.append("НЕТ")
        suffix = f" [{' | '.join(marks)}]" if marks else ""
        print(f"- ID {card_id}, score={card.get('score')}, rank={card.get('rank')}{suffix}")


def choose_best_card(
    opened_cards: List[Dict[str, Any]],
    priority_ids: Set[int],
    owned_ids: Set[int],
    same_priority_rule: int,
    all_owned_rule: int,
    no_priority_rule: int,
    priority_owned_rule: int,
) -> Tuple[Dict[str, Any], str]:
    priority_cards = [card for card in opened_cards if int(card["id"]) in priority_ids]
    priority_new = [card for card in priority_cards if int(card["id"]) not in owned_ids]
    priority_owned = [card for card in priority_cards if int(card["id"]) in owned_ids]

    new_any = [card for card in opened_cards if int(card["id"]) not in owned_ids]
    non_priority_new = [
        card for card in opened_cards
        if int(card["id"]) not in priority_ids and int(card["id"]) not in owned_ids
    ]

    if priority_new:
        if len(priority_new) > 1 and same_priority_rule == 1:
            ids = ", ".join(str(card["id"]) for card in priority_new)
            raise StopReason(f"выпало несколько отсутствующих приоритетных карт: {ids}")
        return random.choice(priority_new), "priority_new"

    # Новое условие:
    # приоритетные есть, но все они уже в наличии.
    if priority_cards and not priority_new:
        if priority_owned_rule == 1 and non_priority_new:
            return random.choice(non_priority_new), "priority_owned_non_priority_new"

        if priority_owned_rule == 2:
            if len(priority_owned) > 1 and same_priority_rule == 1:
                ids = ", ".join(str(card["id"]) for card in priority_owned)
                raise StopReason(f"выпало несколько уже имеющихся приоритетных карт: {ids}")
            return random.choice(priority_owned), "priority_owned_duplicate"

    if not priority_cards:
        if no_priority_rule == 1:
            raise StopReason("среди выпавших карт нет ни одной приоритетной")

        if new_any:
            return random.choice(new_any), "no_priority_new_any"

    if new_any:
        return random.choice(new_any), "new_any"

    if all_owned_rule == 1:
        raise StopReason("все выпавшие карты уже имеются")

    if all_owned_rule == 2 and priority_cards:
        if len(priority_cards) > 1 and same_priority_rule == 1:
            ids = ", ".join(str(card["id"]) for card in priority_cards)
            raise StopReason(f"выпало несколько уже имеющихся приоритетных карт: {ids}")
        return random.choice(priority_cards), "priority_duplicate"

    return random.choice(opened_cards), "duplicate_random"

def reason_text(reason: str) -> str:
    return {
        "priority_new": "Выбираю случайную отсутствующую карту из приоритетных.",
        "no_priority_new_any": "Приоритетных карт нет. Выбираю случайную карту, которой ещё нет.",
        "new_any": "Приоритетных отсутствующих нет. Выбираю случайную отсутствующую карту из выпавших.",
        "priority_duplicate": "Все карты уже есть. Выбираю повторку из приоритетных.",
        "duplicate_random": "Все карты уже есть. Выбираю случайную повторку.",
        "priority_owned_non_priority_new": "Приоритетные выпали, но уже есть. Выбираю неприоритетную новую карту.",
        "priority_owned_duplicate": "Приоритетные выпали, но уже есть. Всё равно выбираю приоритетную повторку.",
    }.get(reason, "Выбираю карту.")


def ask_run_settings(session: requests.Session, token: str, user_id: int) -> Dict[str, Any]:
    collection = select_collection(session, token, user_id)
    priority_ids = get_collection_card_ids(collection)
    missing_from_response = get_missing_collection_ids(collection)
    owned_ids = get_owned_ids(session, token, priority_ids)
    missing_ids = priority_ids - owned_ids

    print(f"\nВыбрана коллекция: {collection.get('name')}")
    print(f"Карт в коллекции: {len(priority_ids)}")
    print(f"Отсутствует по данным коллекции: {len(missing_from_response)}")
    print(f"Отсутствует по проверке has_cards: {len(missing_ids)}")

    target_deck_id = ask_int("\nАйди колоды, которую открыть: ", min_value=1)

    available_decks = get_inventory_decks(session, token, user_id, target_deck_id)
    print(f"У тебя есть таких неоткрытых колод: {len(available_decks)}")

    open_count = ask_int("Сколько колод открыть? 0 = открыть все доступные: ", min_value=0)

    print("\nЕсли есть несколько приоритетных карт с одинаковым статусом, что делать?")
    print("1) Остановка")
    print("2) Рандомный выбор")
    same_priority_rule = ask_int("Выбор 1/2: ", allowed={1, 2})

    print("\nЕсли все карты, которые предлагает система, уже имеются, что делать?")
    print("1) Остановка")
    print("2) Рандомный выбор с приоритетом")
    print("3) Рандомный выбор")
    all_owned_rule = ask_int("Выбор 1/2/3: ", allowed={1, 2, 3})

    print("\nЕсли среди предлагаемых карт нет ни одной приоритетной, что делать?")
    print("1) Остановка")
    print("2) Выбрать рандомно карту, которой ещё нет в наличии")
    no_priority_rule = ask_int("Выбор 1/2: ", allowed={1, 2})

    return {
        "collection": collection,
        "priority_ids": priority_ids,
        "owned_ids": owned_ids,
        "target_deck_id": target_deck_id,
        "open_count": open_count,
        "same_priority_rule": same_priority_rule,
        "all_owned_rule": all_owned_rule,
        "no_priority_rule": no_priority_rule,
    }


def count_manual_opened_decks_after_pause(
    session: requests.Session,
    token: str,
    user_id: int,
    target_deck_id: int,
) -> int:
    """
    Считает только те колоды, которые исчезли именно во время паузы.

    Важно: снимок `before` делается уже после остановки программы. Поэтому сюда
    не попадают колоды, которые программа сама успела открыть в текущем проходе,
    и не возникает ложного зачёта 7-9 штук вместо одной ручной.
    """
    available_before_enter = set(get_inventory_decks(session, token, user_id, target_deck_id))

    input("Нажми Enter, чтобы заново собрать доступные колоды и продолжить по этим же настройкам...")

    available_after_enter = set(get_inventory_decks(session, token, user_id, target_deck_id))
    disappeared = available_before_enter - available_after_enter

    if disappeared:
        ids_text = ", ".join(map(str, sorted(disappeared)))
        print(f"Учтены самостоятельно открытые во время паузы колоды: {len(disappeared)} шт. inventory_id: {ids_text}")

    return len(disappeared)


def refresh_owned_ids(session: requests.Session, token: str, settings: Dict[str, Any]) -> None:
    settings["owned_ids"] = get_owned_ids(session, token, settings["priority_ids"])


def ask_repeat_mode() -> int:
    print("\nЧто дальше?")
    print("1) Повторить операцию с теми же настройками")
    print("2) Начать заново с выбора коллекции")
    return ask_int("Выбор 1/2: ", allowed={1, 2})


def process_decks(session: requests.Session, token: str, user_id: int, settings: Dict[str, Any]) -> bool:
    opened_total = 0
    target_count = settings["open_count"]

    while target_count == 0 or opened_total < target_count:
        inventory_deck_ids = get_inventory_decks(session, token, user_id, settings["target_deck_id"])
        remaining = "все доступные" if target_count == 0 else str(target_count - opened_total)
        print(f"\nНайдено доступных колод deck.id={settings['target_deck_id']}: {len(inventory_deck_ids)}. Нужно открыть: {remaining}.")

        if not inventory_deck_ids:
            if target_count == 0:
                print("\nГотово: открыты все доступные подходящие колоды.")
                return True
            print("Нет подходящих неоткрытых колод.")
            return False

        for inventory_id in inventory_deck_ids:
            if target_count != 0 and opened_total >= target_count:
                print(f"\nГотово: открыто указанное количество колод: {opened_total}.")
                return True

            print(f"\nОткрываю колоду inventory_id={inventory_id}")
            opened_cards = open_deck(session, token, inventory_id)
            print_opened_cards(opened_cards, settings["priority_ids"], settings["owned_ids"])

            try:
                selected_card, reason = choose_best_card(
                    opened_cards=opened_cards,
                    priority_ids=settings["priority_ids"],
                    owned_ids=settings["owned_ids"],
                    same_priority_rule=settings["same_priority_rule"],
                    all_owned_rule=settings["all_owned_rule"],
                    no_priority_rule=settings["no_priority_rule"],
                )
            except StopReason as exc:
                # Колода уже была открыта через API, даже если выбор карты остановлен правилом.
                # Поэтому она тоже должна входить в общий лимит открытий.
                opened_total += 1
                print(f"\nОстановка по правилу: {exc}")
                print(f"Эта колода уже открыта программой и засчитана. Открыто суммарно: {opened_total}.")

                manual_opened = count_manual_opened_decks_after_pause(
                    session=session,
                    token=token,
                    user_id=user_id,
                    target_deck_id=settings["target_deck_id"],
                )
                opened_total += manual_opened
                refresh_owned_ids(session, token, settings)

                if target_count != 0 and opened_total >= target_count:
                    if opened_total == target_count:
                        print(f"\nГотово: суммарно открыто указанное количество колод: {opened_total}.")
                    else:
                        print(f"\nСуммарно открыто {opened_total}, это больше указанного количества {target_count}. Лишние ручные открытия отменить нельзя.")
                    return True

                break

            selected_id = int(selected_card["id"])
            print(f"{reason_text(reason)} ID: {selected_id}")
            result = choose_card(session, token, inventory_id, selected_id)
            settings["owned_ids"].add(selected_id)
            opened_total += 1
            print(f"Карта {selected_id} выбрана. Открыто суммарно по этим настройкам: {opened_total}.")

            if result is not None:
                print("Ответ choose:")
                print(json.dumps(result, ensure_ascii=False, indent=2))

    print(f"\nГотово: открыто указанное количество колод: {opened_total}.")
    return True

def filter_available_cards_for_account(
    opened_cards: List[Dict[str, Any]],
    is_premium: bool,
) -> List[Dict[str, Any]]:
    if is_premium:
        return opened_cards

    # Без премиума 4-я карта недоступна для выбора.
    return opened_cards[:3]

def log_opened_cards(
    opened_cards: List[Dict[str, Any]],
    priority_ids: Set[int],
    owned_ids: Set[int],
    log,
) -> None:
    log("Выпали карты:")
    for card in opened_cards:
        card_id = int(card["id"])
        marks = []
        if card_id in priority_ids:
            marks.append("ПРИОРИТЕТ")
        if card_id in owned_ids:
            marks.append("УЖЕ ЕСТЬ")
        else:
            marks.append("НЕТ")
        suffix = f" [{' | '.join(marks)}]" if marks else ""
        log(f"- ID {card_id}, score={card.get('score')}, rank={card.get('rank')}{suffix}")


def run_opening_process(
    config: RunConfig,
    log=None,
    should_stop=None,
    choose_card_manually: Optional[
        Callable[[List[Dict[str, Any]], Set[int], Set[int], str], Optional[int]]
    ] = None,
) -> Dict[str, Any]:
    """
    Версия процесса для PySide6.

    Не использует input(), print() и sys.exit().
    Сообщения отправляются в log(...).
    should_stop() нужен для кнопки остановки.
    """
    if log is None:
        log = lambda text: None
    if should_stop is None:
        should_stop = lambda: False

    session = requests.Session()

    priority_ids = get_collection_card_ids(config.collection)
    owned_ids = get_owned_ids(session, config.token, priority_ids)
    missing_ids = priority_ids - owned_ids

    collection_name = config.collection.get("name") or config.collection.get("title") or f"id={config.collection.get('id')}"
    log(f"Выбрана коллекция: {collection_name}")
    log(f"Карт в коллекции: {len(priority_ids)}")
    log(f"Отсутствует по проверке has_cards: {len(missing_ids)}")

    opened_total = 0
    target_count = config.open_count
    stop_reason = None

    while target_count == 0 or opened_total < target_count:
        if should_stop():
            stop_reason = "остановлено пользователем"
            log("Остановлено пользователем.")
            break

        inventory_deck_ids = get_inventory_decks(
            session=session,
            token=config.token,
            user_id=config.user_id,
            target_deck_id=config.target_deck_id,
        )

        remaining = "все доступные" if target_count == 0 else str(target_count - opened_total)
        log("")
        log(
            f"Найдено доступных колод deck.id={config.target_deck_id}: "
            f"{len(inventory_deck_ids)}. Нужно открыть: {remaining}."
        )

        if not inventory_deck_ids:
            stop_reason = "нет подходящих неоткрытых колод"
            log("Нет подходящих неоткрытых колод.")
            break

        for inventory_id in inventory_deck_ids:
            if should_stop():
                stop_reason = "остановлено пользователем"
                log("Остановлено пользователем.")
                break

            if target_count != 0 and opened_total >= target_count:
                break

            log("")
            log(f"Открываю колоду inventory_id={inventory_id}")

            opened_cards_raw = open_deck(session, config.token, inventory_id)

            if not config.is_premium and len(opened_cards_raw) > 3:
                log("Премиум аккаунт: нет. 4-я карта недоступна и не будет учитываться.")

            opened_cards = filter_available_cards_for_account(
                opened_cards_raw,
                config.is_premium,
            )

            log_opened_cards(opened_cards, priority_ids, owned_ids, log)

            try:
                selected_card, reason = choose_best_card(
                    opened_cards=opened_cards,
                    priority_ids=priority_ids,
                    owned_ids=owned_ids,
                    same_priority_rule=config.same_priority_rule,
                    all_owned_rule=config.all_owned_rule,
                    no_priority_rule=config.no_priority_rule,
                    priority_owned_rule=config.priority_owned_rule,
                )
            except StopReason as exc:
                log("")
                log(f"Остановка по правилу: {exc}")

                if choose_card_manually is None:
                    opened_total += 1
                    stop_reason = str(exc)
                    log(f"Колода уже открыта через API и засчитана. Открыто суммарно: {opened_total}.")
                    return {
                        "opened_total": opened_total,
                        "stop_reason": stop_reason,
                    }

                log("Открываю окно ручного выбора карты...")
                manual_card_id = choose_card_manually(
                    opened_cards,
                    priority_ids,
                    owned_ids,
                    str(exc),
                )

                if manual_card_id is None:
                    opened_total += 1
                    stop_reason = "ручной выбор отменён"
                    log("Ручной выбор отменён.")
                    log(f"Колода уже открыта через API и засчитана. Открыто суммарно: {opened_total}.")
                    return {
                        "opened_total": opened_total,
                        "stop_reason": stop_reason,
                    }

                selected_id = int(manual_card_id)
                log(f"Выбрана карта вручную. ID: {selected_id}")
            else:
                selected_id = int(selected_card["id"])
                log(f"{reason_text(reason)} ID: {selected_id}")

            result = choose_card(session, config.token, inventory_id, selected_id)
            owned_ids.add(selected_id)
            opened_total += 1

            log(f"Карта {selected_id} выбрана. Открыто суммарно: {opened_total}.")

            if result is not None:
                log("Ответ choose:")
                log(json.dumps(result, ensure_ascii=False, indent=2))

        if stop_reason:
            break

    if stop_reason is None:
        log("")
        log(f"Готово. Открыто колод: {opened_total}.")

    return {
        "opened_total": opened_total,
        "stop_reason": stop_reason,
    }


def main() -> None:
    token, user_id = read_settings()
    session = requests.Session()
    settings: Optional[Dict[str, Any]] = None

    while True:
        if settings is None:
            settings = ask_run_settings(session, token, user_id)
        else:
            refresh_owned_ids(session, token, settings)

        completed = process_decks(session, token, user_id, settings)

        if completed:
            repeat_mode = ask_repeat_mode()
            if repeat_mode == 1:
                print("\nПовторяю с теми же настройками.")
                continue

        settings = None
        print("\nНачинаю заново: выбор коллекции и настроек открытия.")

