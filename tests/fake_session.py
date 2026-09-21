"""Fake browser session for server tests: the DOM model the engine touches.

Only the Playwright session is faked - the runner, PageOps, discovery,
corrections, transport, decision gate, and manual backend are all real.
The fake models the review app's behavior after a verdict: the reviewer SPA
advances to the next card, simulated by checking the shared FakeQAApp's
verdict state before every read. Cards are decided exactly once, so a
resumed worker (new session, same app) advances past already-decided cards
instead of serving them again.

The HTTP surface reuses ``FakeQAApp`` from tests.test_pipeline (the same
in-memory QA app QAClient tests run against).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from types import SimpleNamespace

from tests.test_pipeline import FakeQAApp

_NAME_RE = re.compile(r"name='([^']+)'")
_VALUE_RE = re.compile(r"value='([^']*)'")


class Card:
    """One review card as the fake DOM serves it."""

    def __init__(
        self,
        unit_id: str,
        record_id: str,
        company_name: str,
        fields: dict[str, str],
        links: list[tuple[str, str]] | None = None,
    ) -> None:
        self.unit_id = unit_id
        self.record_id = record_id
        self.company_name = company_name
        self.fields = dict(fields)
        self.links = links or []

    @property
    def nonce(self) -> str:
        return f"n-{self.unit_id}"


def default_cards() -> list[Card]:
    return [
        Card(
            "101",
            "MST-2001",
            "Acme Rice",
            {"company_name": "Acme Rice", "website_url": "https://acmerice.example"},
        ),
        Card(
            "102",
            "MST-2002",
            "Beacon Packaging",
            {
                "company_name": "Beacon Packaging",
                "website_url": "https://beaconpackaging.example",
            },
        ),
    ]


class _Element:
    """A single DOM element with just the Playwright methods the engine uses."""

    def __init__(self, value: str, checked: bool = False) -> None:
        self._value = value
        self._checked = checked
        self.filled: str | None = None

    def input_value(self) -> str:
        return self._value

    def get_attribute(self, name: str) -> str | None:
        if name in ("value", "href"):
            return self._value or None
        return None

    def inner_text(self) -> str:
        return self._value

    def is_checked(self) -> bool:
        return self._checked

    def is_visible(self) -> bool:
        return True

    def fill(self, value: str) -> None:
        self.filled = value

    def count(self) -> int:
        return 1

    @property
    def first(self) -> "_Element":
        return self

    def nth(self, _i: int) -> "_Element":
        return self

    def locator(self, _selector: str) -> "_ListLocator":
        return _ListLocator([])


_EMPTY = _Element("")


class _ListLocator:
    """A locator over a list of elements: count / nth / first."""

    def __init__(self, elements: list[_Element]) -> None:
        self._elements = elements

    def count(self) -> int:
        return len(self._elements)

    @property
    def first(self) -> _Element:
        return self._elements[0] if self._elements else _EMPTY

    def nth(self, i: int) -> _Element:
        return self._elements[i]

    def locator(self, _selector: str) -> "_ListLocator":
        return _ListLocator([])


class _FormLocator:
    """The ancestor form of one hidden field input, with its edit controls."""

    def __init__(self, page: "FakePage", key: str) -> None:
        self._page = page
        self._key = key

    def count(self) -> int:
        return 1

    def locator(self, selector: str) -> _ListLocator:
        match = _NAME_RE.search(selector)
        name = match.group(1) if match else selector
        if name == "new_value":  # textareas are not modeled; text inputs only
            value = self._page.card.fields.get(self._key)
            return _ListLocator([_Element(value)] if value is not None else [])
        return _ListLocator([])  # new_value_multi: no checkboxes modeled


class _HiddenFieldInput:
    """One hidden input: input_value() is the field key; ancestor form."""

    def __init__(self, page: "FakePage", key: str) -> None:
        self._page = page
        self._key = key

    def input_value(self) -> str:
        return self._key

    def get_attribute(self, _name: str) -> str | None:
        return self._key

    def locator(self, _selector: str) -> _FormLocator:
        return _FormLocator(self._page, self._key)


class _HiddenFieldList:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    def count(self) -> int:
        return len(self._page.card.fields)

    def nth(self, i: int) -> _HiddenFieldInput:
        keys = list(self._page.card.fields)
        return _HiddenFieldInput(self._page, keys[i])

    def locator(self, _selector: str) -> _ListLocator:
        return _ListLocator([])


class _SingleValueLocator:
    """unit_id / nonce style singletons: count, first, get_attribute."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def count(self) -> int:
        return 1 if self._value is not None else 0

    @property
    def first(self) -> _Element:
        return _Element(self._value or "")

    def nth(self, _i: int) -> _Element:
        return _Element(self._value or "")

    def get_attribute(self, _name: str) -> str | None:
        return self._value


class _BodyLocator:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    def count(self) -> int:
        return 1

    def inner_text(self) -> str:
        card = self._page.card
        lines = [card.company_name, card.record_id]
        lines += [f"{k}: {v}" for k, v in card.fields.items()]
        return "\n".join(lines)


class _LinkLocator:
    def __init__(self, elements: list[_Element]) -> None:
        self._elements = elements

    def count(self) -> int:
        return len(self._elements)

    def nth(self, i: int) -> _Element:
        return self._elements[i]

    def locator(self, _selector: str) -> _ListLocator:
        return _ListLocator([])


class FakePage:
    """Serves the declared cards in order; advances past decided cards."""

    def __init__(self, qa_app: FakeQAApp, cards: list[Card]) -> None:
        self._qa = qa_app
        self._cards = cards
        self._index = 0
        self.context = SimpleNamespace(request=qa_app)

    # -- advance simulation ------------------------------------------------
    def _decided(self, card: Card) -> bool:
        state = self._qa.cards.get(card.unit_id) or {}
        return state.get("verdict") is not None

    @property
    def card(self) -> Card:
        while self._index < len(self._cards) and self._decided(self._cards[self._index]):
            self._index += 1
        if self._index >= len(self._cards):
            raise AssertionError("FakePage ran out of cards; test declared too few")
        return self._cards[self._index]

    # -- the surface the engine touches ------------------------------------
    def is_closed(self) -> bool:
        return False

    def goto(self, _url: str, **_kw: object) -> None:
        return None

    def wait_for_load_state(self, _state: str = "load", **_kw: object) -> None:
        return None

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _js: str) -> list[dict[str, str]]:
        return [{"field": k, "kind": "text"} for k in self.card.fields]

    def locator(self, selector: str) -> object:
        if selector == "body":
            return _BodyLocator(self)
        if selector == "a":
            return _LinkLocator([_Element(href) for _t, href in self.card.links])
        if selector.startswith("input[type='hidden'][name='field']"):
            if "value='" in selector:
                match = _VALUE_RE.search(selector)
                key = match.group(1) if match else ""
                present = key in self.card.fields
                return _SingleValueLocator(key if present else None)
            return _HiddenFieldList(self)
        match = _NAME_RE.search(selector)
        name = match.group(1) if match else ""
        if name in ("unit_id", "nonce"):
            value = self.card.unit_id if name == "unit_id" else self.card.nonce
            return _SingleValueLocator(value)
        return _SingleValueLocator(None)  # chrome (save buttons, note inputs, etc.)


class FakeSessionFactory:
    """Builds one shared QA app; each call yields a fresh (context, page)."""

    def __init__(self, cards: list[Card] | None = None) -> None:
        self.qa_app = FakeQAApp()
        self.cards = cards if cards is not None else default_cards()
        self.qa_app.cards = {
            c.unit_id: {"unit_id": c.unit_id, "nonce": c.nonce, "status": "pending", "verdict": None}
            for c in self.cards
        }

    def __call__(self):
        qa = self.qa_app
        cards = self.cards

        @contextmanager
        def session():
            yield SimpleNamespace(request=qa), FakePage(qa, cards)

        return session()
