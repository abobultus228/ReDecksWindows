import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from PySide6.QtGui import QPixmap
from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

from logic_ver2 import (
    DecksLogicError,
    RunConfig,
    get_available_deck_types,
    get_rare_collections,
    run_opening_process,
)


SETTINGS_FILE = Path("settings.json")


def collection_title(collection: Dict[str, Any]) -> str:
    """Красивое имя коллекции для выпадающего списка."""
    name = collection.get("name") or collection.get("title") or "Без названия"
    collection_id = collection.get("id", "?")
    percent = collection.get("percent")
    if percent is None:
        return f"{name} | id={collection_id}"
    return f"{name} | id={collection_id} | {percent}%"



def deck_title(deck_info: Dict[str, Any]) -> str:
    name = deck_info.get("name") or "Без названия"
    deck_id = deck_info.get("id", "?")
    count = deck_info.get("count", 0)
    return f"{name} | ID {deck_id} | доступно: {count}"



class ManualChoiceDialog(QDialog):
    def __init__(
        self,
        cards: List[Dict[str, Any]],
        priority_ids: set,
        owned_ids: set,
        reason: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ручной выбор карты")
        self.resize(1200, 560)

        self.cards = cards
        self.priority_ids = priority_ids
        self.owned_ids = owned_ids
        self.selected_card_id: Optional[int] = None
        self.pixmaps: Dict[int, QPixmap] = {}
        self.image_labels: Dict[int, QLabel] = {}

        layout = QVBoxLayout(self)

        title = QLabel(f"Остановка по правилу: {reason}")
        title.setWordWrap(True)
        layout.addWidget(title)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(0)

        self.button_group = QButtonGroup(self)

        for card in cards:
            card_id = int(card["id"])
            card_widget = QWidget()
            card_layout = QVBoxLayout(card_widget)
            card_layout.setContentsMargins(8, 8, 8, 8)

            is_priority = card_id in priority_ids
            is_owned = card_id in owned_ids

            status_parts = ["ПРИОРИТЕТНАЯ" if is_priority else "НЕ приоритетная"]
            status_parts.append("уже есть" if is_owned else "новая")
            status_label = QLabel(" | ".join(status_parts))
            status_label.setAlignment(Qt.AlignCenter)
            status_label.setWordWrap(True)

            image_label = QLabel("Загрузка изображения...")
            image_label.setAlignment(Qt.AlignCenter)
            image_label.setMinimumSize(180, 270)

            radio = QRadioButton(f"ID {card_id}")
            radio.setProperty("card_id", card_id)
            self.button_group.addButton(radio)

            if self.button_group.buttons() and len(self.button_group.buttons()) == 1:
                radio.setChecked(True)
                self.selected_card_id = card_id

            card_layout.addWidget(status_label)
            card_layout.addWidget(image_label)
            card_layout.addWidget(radio, alignment=Qt.AlignCenter)

            self.image_labels[card_id] = image_label
            self.pixmaps[card_id] = self._download_pixmap(card)

            cards_row.addWidget(card_widget, 1)

        self.button_group.buttonClicked.connect(self._on_card_selected)
        layout.addLayout(cards_row)

        buttons = QHBoxLayout()
        cancel_button = QPushButton("Отмена")
        confirm_button = QPushButton("Подтвердить выбор")
        cancel_button.clicked.connect(self.reject)
        confirm_button.clicked.connect(self.accept)

        buttons.addStretch()
        buttons.addWidget(cancel_button)
        buttons.addWidget(confirm_button)
        layout.addLayout(buttons)

        self._rescale_images()

    def _on_card_selected(self, button) -> None:
        self.selected_card_id = int(button.property("card_id"))

    def _download_pixmap(self, card: Dict[str, Any]) -> QPixmap:
        cover = card.get("cover") or {}
        path = cover.get("mid") or cover.get("high")
        pixmap = QPixmap()

        if not path:
            return pixmap

        url = path if str(path).startswith("http") else f"https://remanga.org{path}"

        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            pixmap.loadFromData(response.content)
        except Exception:
            pass

        return pixmap

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale_images()

    def _rescale_images(self) -> None:
        if not self.image_labels:
            return

        image_width = max(120, int(self.width() * 0.20))
        image_height = int(image_width * 1.5)

        for card_id, label in self.image_labels.items():
            label.setFixedSize(image_width, image_height)
            pixmap = self.pixmaps.get(card_id)
            if pixmap and not pixmap.isNull():
                label.setPixmap(
                    pixmap.scaled(
                        image_width,
                        image_height,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
            else:
                label.setText("Не удалось загрузить изображение")



class CollectionsWorker(QObject):
    finished = Signal(list)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, token: str, user_id: int) -> None:
        super().__init__()
        self.token = token
        self.user_id = user_id

    @Slot()
    def run(self) -> None:
        try:
            self.log.emit("Загружаю коллекции...")
            session = requests.Session()
            collections = get_rare_collections(session, self.token, self.user_id)
            self.log.emit(f"Коллекций загружено: {len(collections)}")
            self.finished.emit(collections)
        except Exception as exc:
            self.error.emit(str(exc))


class AvailableDecksWorker(QObject):
    finished = Signal(list)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, token: str, user_id: int) -> None:
        super().__init__()
        self.token = token
        self.user_id = user_id

    @Slot()
    def run(self) -> None:
        try:
            self.log.emit("Загружаю доступные колоды...")
            session = requests.Session()
            decks = get_available_deck_types(session, self.token, self.user_id)
            self.log.emit(f"Типов доступных колод загружено: {len(decks)}")
            self.finished.emit(decks)
        except Exception as exc:
            self.error.emit(str(exc))


class OpenDecksWorker(QObject):
    finished = Signal(dict)
    error = Signal(str)
    log = Signal(str)
    manual_choice_requested = Signal(object)

    def __init__(self, config: RunConfig) -> None:
        super().__init__()
        self.config = config
        self._stop_requested = False
        self._choice_event = threading.Event()
        self._choice_result: Optional[int] = None

    def request_stop(self) -> None:
        self._stop_requested = True

    def should_stop(self) -> bool:
        return self._stop_requested

    def choose_card_manually(
        self,
        cards: List[Dict[str, Any]],
        priority_ids: set,
        owned_ids: set,
        reason: str,
    ) -> Optional[int]:
        self._choice_result = None
        self._choice_event.clear()
        self.manual_choice_requested.emit({
            "cards": cards,
            "priority_ids": priority_ids,
            "owned_ids": owned_ids,
            "reason": reason,
        })
        self._choice_event.wait()
        return self._choice_result

    @Slot(object)
    def set_manual_choice_result(self, card_id: Optional[int]) -> None:
        self._choice_result = card_id
        self._choice_event.set()

    @Slot()
    def run(self) -> None:
        try:
            result = run_opening_process(
                self.config,
                log=self.log.emit,
                should_stop=self.should_stop,
                choose_card_manually=self.choose_card_manually,
            )
            self.finished.emit(result)
        except DecksLogicError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            self.error.emit(f"Неожиданная ошибка: {exc}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("Remanga Deck Opener")
        self.resize(900, 650)

        self.collections: List[Dict[str, Any]] = []
        self.available_decks: List[Dict[str, Any]] = []
        self.collections_thread: Optional[QThread] = None
        self.collections_worker: Optional[CollectionsWorker] = None
        self.decks_thread: Optional[QThread] = None
        self.decks_worker: Optional[AvailableDecksWorker] = None
        self.open_thread: Optional[QThread] = None
        self.open_worker: Optional[OpenDecksWorker] = None

        self.tabs = QTabWidget()
        self.settings_tab = QWidget()
        self.process_tab = QWidget()
        self.tabs.addTab(self.settings_tab, "Настройки")
        self.tabs.addTab(self.process_tab, "Процесс")
        self.setCentralWidget(self.tabs)

        self._build_settings_tab()
        self._build_process_tab()
        self._load_settings()

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_tab)

        api_group = QGroupBox("Данные аккаунта")
        api_form = QFormLayout(api_group)

        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setPlaceholderText("Bearer token без слова Bearer")

        self.user_id_input = QLineEdit()
        self.user_id_input.setPlaceholderText("Например: 123456")
        self.premium_checkbox = QCheckBox("Да")

        self.load_collections_button = QPushButton("Загрузить коллекции")
        self.load_collections_button.clicked.connect(self.load_collections)

        self.load_decks_button = QPushButton("Загрузить доступные колоды")
        self.load_decks_button.clicked.connect(self.load_available_decks)

        api_form.addRow("Remanga token:", self.token_input)
        api_form.addRow("User ID:", self.user_id_input)
        api_form.addRow("Премиум аккаунт:", self.premium_checkbox)
        api_form.addRow("", self.load_collections_button)
        api_form.addRow("", self.load_decks_button)

        collection_group = QGroupBox("Коллекция и колода")
        collection_form = QFormLayout(collection_group)

        self.collection_combo = QComboBox()
        self.collection_combo.setPlaceholderText("Сначала загрузите коллекции")

        self.deck_combo = QComboBox()
        self.deck_combo.setPlaceholderText("Сначала загрузите доступные колоды")

        self.deck_id_input = QLineEdit()
        self.deck_id_input.setPlaceholderText("Резерв: deck.id вручную, если список не загружен")

        self.open_count_spin = QSpinBox()
        self.open_count_spin.setRange(0, 100000)
        self.open_count_spin.setValue(1)
        self.open_count_spin.setSpecialValueText("1")

        collection_form.addRow("Коллекция:", self.collection_combo)
        collection_form.addRow("Доступная колода:", self.deck_combo)
        collection_form.addRow("ID колоды вручную:", self.deck_id_input)
        collection_form.addRow("Сколько открыть:", self.open_count_spin)

        rules_group = QGroupBox("Правила выбора")
        rules_form = QFormLayout(rules_group)

        self.priority_owned_combo = QComboBox()
        self.priority_owned_combo.addItem("Выбрать неприоритетную карту, которой нет", 1)
        self.priority_owned_combo.addItem("Всё равно выбрать приоритетную", 2)

        self.same_priority_combo = QComboBox()
        self.same_priority_combo.addItem("Остановиться", 1)
        self.same_priority_combo.addItem("Выбрать случайную", 2)

        self.all_owned_combo = QComboBox()
        self.all_owned_combo.addItem("Остановиться", 1)
        self.all_owned_combo.addItem("Выбрать приоритетную повторку", 2)
        self.all_owned_combo.addItem("Выбрать случайную повторку", 3)

        self.no_priority_combo = QComboBox()
        self.no_priority_combo.addItem("Остановиться", 1)
        self.no_priority_combo.addItem("Выбрать случайную новую карту", 2)

        rules_form.addRow("Приоритетные выпали, но уже есть:", self.priority_owned_combo)
        rules_form.addRow("Несколько приоритетных:", self.same_priority_combo)
        rules_form.addRow("Все выпавшие уже есть:", self.all_owned_combo)
        rules_form.addRow("Нет приоритетных среди выпавших:", self.no_priority_combo)

        buttons = QHBoxLayout()
        self.save_settings_button = QPushButton("Сохранить настройки")
        self.save_settings_button.clicked.connect(self.save_settings)

        self.start_button = QPushButton("Запустить")
        self.start_button.clicked.connect(self.start_opening)

        buttons.addWidget(self.save_settings_button)
        buttons.addStretch()
        buttons.addWidget(self.start_button)

        layout.addWidget(api_group)
        layout.addWidget(collection_group)
        layout.addWidget(rules_group)
        layout.addLayout(buttons)
        layout.addStretch()

    def _build_process_tab(self) -> None:
        layout = QVBoxLayout(self.process_tab)

        self.status_label = QLabel("Готово к запуску.")
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)

        buttons = QHBoxLayout()
        self.clear_log_button = QPushButton("Очистить лог")
        self.clear_log_button.clicked.connect(self.log_output.clear)

        self.stop_button = QPushButton("Остановить")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_opening)

        buttons.addWidget(self.clear_log_button)
        buttons.addStretch()
        buttons.addWidget(self.stop_button)

        layout.addWidget(self.status_label)
        layout.addWidget(self.log_output)
        layout.addLayout(buttons)

    def append_log(self, text: str) -> None:
        self.log_output.appendPlainText(text)

    def _token(self) -> str:
        return self.token_input.text().strip()

    def _user_id(self) -> int:
        text = self.user_id_input.text().strip()
        if not text.isdigit():
            raise ValueError("User ID должен быть числом.")
        return int(text)

    def _deck_id(self) -> int:
        selected_deck = self.deck_combo.currentData()
        if selected_deck and selected_deck.get("id") is not None:
            return int(selected_deck["id"])

        text = self.deck_id_input.text().strip()
        if not text.isdigit():
            raise ValueError("Выберите колоду из списка или введите ID колоды вручную.")
        return int(text)

    def _selected_collection(self) -> Dict[str, Any]:
        collection = self.collection_combo.currentData()
        if not collection:
            raise ValueError("Сначала выберите коллекцию.")
        return collection

    def _build_config(self) -> RunConfig:
        token = self._token()
        if not token:
            raise ValueError("Введите Remanga token.")

        return RunConfig(
            token=token,
            user_id=self._user_id(),
            collection=self._selected_collection(),
            target_deck_id=self._deck_id(),
            open_count=self.open_count_spin.value(),
            priority_owned_rule=self.priority_owned_combo.currentData(),
            is_premium=self.premium_checkbox.isChecked(),
            same_priority_rule=self.same_priority_combo.currentData(),
            all_owned_rule=self.all_owned_combo.currentData(),
            no_priority_rule=self.no_priority_combo.currentData(),
        )

    def load_collections(self) -> None:
        try:
            token = self._token()
            if not token:
                raise ValueError("Введите Remanga token.")
            user_id = self._user_id()
        except Exception as exc:
            QMessageBox.warning(self, "Проверьте данные", str(exc))
            return

        self.load_collections_button.setEnabled(False)
        self.status_label.setText("Загрузка коллекций...")
        self.tabs.setCurrentWidget(self.process_tab)

        self.collections_thread = QThread()
        self.collections_worker = CollectionsWorker(token, user_id)
        self.collections_worker.moveToThread(self.collections_thread)

        self.collections_thread.started.connect(self.collections_worker.run)
        self.collections_worker.log.connect(self.append_log)
        self.collections_worker.finished.connect(self.on_collections_loaded)
        self.collections_worker.error.connect(self.on_worker_error)

        self.collections_worker.finished.connect(self.collections_thread.quit)
        self.collections_worker.error.connect(self.collections_thread.quit)
        self.collections_thread.finished.connect(self.collections_worker.deleteLater)
        self.collections_thread.finished.connect(self.collections_thread.deleteLater)
        self.collections_thread.finished.connect(lambda: setattr(self, "collections_thread", None))
        self.collections_thread.finished.connect(lambda: setattr(self, "collections_worker", None))
        self.collections_thread.finished.connect(lambda: self.load_collections_button.setEnabled(True))

        self.collections_thread.start()

    def on_collections_loaded(self, collections: List[Dict[str, Any]]) -> None:
        self.collections = collections
        self.collection_combo.clear()

        for collection in collections:
            self.collection_combo.addItem(collection_title(collection), collection)

        self.status_label.setText(f"Коллекции загружены: {len(collections)}")
        self.tabs.setCurrentWidget(self.settings_tab)


    def load_available_decks(self) -> None:
        try:
            token = self._token()
            if not token:
                raise ValueError("Введите Remanga token.")
            user_id = self._user_id()
        except Exception as exc:
            QMessageBox.warning(self, "Проверьте данные", str(exc))
            return

        self.load_decks_button.setEnabled(False)
        self.status_label.setText("Загрузка доступных колод...")
        self.tabs.setCurrentWidget(self.process_tab)

        self.decks_thread = QThread()
        self.decks_worker = AvailableDecksWorker(token, user_id)
        self.decks_worker.moveToThread(self.decks_thread)

        self.decks_thread.started.connect(self.decks_worker.run)
        self.decks_worker.log.connect(self.append_log)
        self.decks_worker.finished.connect(self.on_available_decks_loaded)
        self.decks_worker.error.connect(self.on_worker_error)

        self.decks_worker.finished.connect(self.decks_thread.quit)
        self.decks_worker.error.connect(self.decks_thread.quit)
        self.decks_thread.finished.connect(self.decks_worker.deleteLater)
        self.decks_thread.finished.connect(self.decks_thread.deleteLater)
        self.decks_thread.finished.connect(lambda: setattr(self, "decks_thread", None))
        self.decks_thread.finished.connect(lambda: setattr(self, "decks_worker", None))
        self.decks_thread.finished.connect(lambda: self.load_decks_button.setEnabled(True))

        self.decks_thread.start()

    def on_available_decks_loaded(self, decks: List[Dict[str, Any]]) -> None:
        self.available_decks = decks
        self.deck_combo.clear()

        for deck in decks:
            self.deck_combo.addItem(deck_title(deck), deck)

        self.status_label.setText(f"Доступные колоды загружены: {len(decks)}")
        self.tabs.setCurrentWidget(self.settings_tab)

        saved_deck_id = self.deck_id_input.text().strip()
        if saved_deck_id.isdigit():
            for index in range(self.deck_combo.count()):
                deck = self.deck_combo.itemData(index)
                if deck and int(deck.get("id", -1)) == int(saved_deck_id):
                    self.deck_combo.setCurrentIndex(index)
                    break

    def start_opening(self) -> None:
        try:
            config = self._build_config()
        except Exception as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))
            return

        self.save_settings()
        self.tabs.setCurrentWidget(self.process_tab)
        self.status_label.setText("Процесс запущен...")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.load_collections_button.setEnabled(False)
        self.load_decks_button.setEnabled(False)

        self.open_thread = QThread()
        self.open_worker = OpenDecksWorker(config)
        self.open_worker.moveToThread(self.open_thread)

        self.open_thread.started.connect(self.open_worker.run)
        self.open_worker.log.connect(self.append_log)
        self.open_worker.manual_choice_requested.connect(self.on_manual_choice_requested)
        self.open_worker.finished.connect(self.on_opening_finished)
        self.open_worker.error.connect(self.on_worker_error)

        self.open_worker.finished.connect(self.open_thread.quit)
        self.open_worker.error.connect(self.open_thread.quit)
        self.open_thread.finished.connect(self.open_worker.deleteLater)
        self.open_thread.finished.connect(self.open_thread.deleteLater)
        self.open_thread.finished.connect(lambda: setattr(self, "open_thread", None))
        self.open_thread.finished.connect(lambda: setattr(self, "open_worker", None))
        self.open_thread.finished.connect(self._unlock_after_opening)

        self.open_thread.start()

    def stop_opening(self) -> None:
        if self.open_worker:
            self.open_worker.request_stop()
            self.status_label.setText("Останавливаю после текущего действия...")
            self.append_log("Запрошена остановка.")


    def on_manual_choice_requested(self, payload: Dict[str, Any]) -> None:
        if not self.open_worker:
            return

        dialog = ManualChoiceDialog(
            cards=payload["cards"],
            priority_ids=payload["priority_ids"],
            owned_ids=payload["owned_ids"],
            reason=payload["reason"],
            parent=self,
        )

        if dialog.exec() == QDialog.Accepted:
            self.open_worker.set_manual_choice_result(dialog.selected_card_id)
        else:
            self.open_worker.set_manual_choice_result(None)

    def on_opening_finished(self, result: Dict[str, Any]) -> None:
        opened_total = result.get("opened_total", 0)
        stop_reason = result.get("stop_reason")
        if stop_reason:
            self.status_label.setText(f"Остановлено. Открыто: {opened_total}. Причина: {stop_reason}")
        else:
            self.status_label.setText(f"Готово. Открыто: {opened_total}.")

    def on_worker_error(self, message: str) -> None:
        self.status_label.setText("Ошибка.")
        self.append_log("")
        self.append_log(message)
        QMessageBox.critical(self, "Ошибка", message)

    def _unlock_after_opening(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.load_collections_button.setEnabled(True)
        self.load_decks_button.setEnabled(True)

    def save_settings(self) -> None:
        selected_deck = self.deck_combo.currentData()
        manual_deck_id = self.deck_id_input.text().strip()

        if selected_deck and selected_deck.get("id") is not None:
            deck_id_for_settings = str(selected_deck["id"])
        else:
            deck_id_for_settings = manual_deck_id

        data = {
            "token": self.token_input.text(),
            "user_id": self.user_id_input.text(),
            "is_premium": self.premium_checkbox.isChecked(),
            "deck_id": deck_id_for_settings,
            "open_count": self.open_count_spin.value(),
            "priority_owned_rule": self.priority_owned_combo.currentIndex(),
            "same_priority_rule": self.same_priority_combo.currentIndex(),
            "all_owned_rule": self.all_owned_combo.currentIndex(),
            "no_priority_rule": self.no_priority_combo.currentIndex(),
            "collection_id": (
                self.collection_combo.currentData().get("id")
                if self.collection_combo.currentData()
                else None
            ),
        }

        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.append_log("Настройки сохранены.")

    def _load_settings(self) -> None:
        if not SETTINGS_FILE.exists():
            return

        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        self.token_input.setText(data.get("token", ""))
        self.user_id_input.setText(data.get("user_id", ""))
        self.premium_checkbox.setChecked(bool(data.get("is_premium", False)))
        self.deck_id_input.setText(data.get("deck_id", ""))
        self.open_count_spin.setValue(int(data.get("open_count", 0)))

        self.priority_owned_combo.setCurrentIndex(int(data.get("priority_owned_rule", 0)))
        self.same_priority_combo.setCurrentIndex(int(data.get("same_priority_rule", 0)))
        self.all_owned_combo.setCurrentIndex(int(data.get("all_owned_rule", 0)))
        self.no_priority_combo.setCurrentIndex(int(data.get("no_priority_rule", 0)))


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
