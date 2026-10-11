"""Dine-in prices recomputed from the canonical menu, never from the browser.

Mirrors TableTap's lib/menuUtils.ts resolvePrice(): in dine-in mode an item's
DineInPrice (and an option's dineInPriceModifier) wins when it is a usable
number; anything absent or malformed means "same as takeout".
"""
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

TAX_RATE = Decimal('0.13')
MAX_LINES = 30
MAX_QUANTITY = 50
MENU_CACHE_SECONDS = 300

_cache = {'at': 0.0, 'lookup': None}


def _number(value):
    """Decimal for a usable price, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def resolve_price(takeout, dine_in):
    dine = _number(dine_in)
    if dine is not None:
        return dine
    return _number(takeout) or Decimal('0')


def menu_lookup(menu_table, now=None):
    """Menu items keyed by ItemNumber (the id the site sends), cached briefly
    so a busy dinner rush doesn't scan the table on every order."""
    now = time.time() if now is None else now
    if _cache['lookup'] is not None and now - _cache['at'] < MENU_CACHE_SECONDS:
        return _cache['lookup']
    lookup = {}
    scan_kwargs = {}
    while True:
        resp = menu_table.scan(**scan_kwargs)
        for menu_item in resp.get('Items', []):
            item_number = menu_item.get('ItemNumber')
            if item_number is not None:
                lookup[str(item_number)] = menu_item
        if 'LastEvaluatedKey' not in resp:
            break
        scan_kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    _cache.update(at=now, lookup=lookup)
    return lookup


def _selected_options(raw):
    """Options arrive as "Group: Option; Group2: Option2"."""
    selected = {}
    for part in (raw or '').split(';'):
        if ':' in part:
            group_name, option_name = part.split(':', 1)
            selected[group_name.strip()] = option_name.strip()
    return selected


def price_dine_in_items(items, lookup):
    """Return (items with server-side prices, subtotal, total incl. tax).

    Raises ValueError for an empty or oversized order, an unknown item or a
    bad quantity. Prices, names and kitchen locations come from the menu;
    the chosen options and anything else on the line are kept as sent.
    """
    if not items or not isinstance(items, list):
        raise ValueError('No items in order')
    if len(items) > MAX_LINES:
        raise ValueError('Too many items in one order')

    priced = []
    subtotal = Decimal('0')
    for line in items:
        if not isinstance(line, dict):
            raise ValueError('Bad item in order')
        menu_item = lookup.get(str(line.get('id')))
        if not menu_item:
            raise ValueError(f"Unknown menu item: {line.get('name') or line.get('id')}")
        try:
            quantity = int(line.get('quantity', 1))
        except (TypeError, ValueError):
            raise ValueError('Bad quantity')
        if quantity < 1 or quantity > MAX_QUANTITY:
            raise ValueError('Bad quantity')

        unit = resolve_price(menu_item.get('Price'), menu_item.get('DineInPrice'))
        selected = _selected_options(line.get('options'))
        for group in menu_item.get('Options', []) or []:
            chosen = selected.get(group.get('name'))
            if not chosen:
                continue
            for opt in group.get('items', []) or []:
                if opt.get('name') == chosen:
                    unit += resolve_price(opt.get('priceModifier'), opt.get('dineInPriceModifier'))
                    break

        unit = unit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        line_total = unit * quantity
        subtotal += line_total
        priced.append({
            **line,
            'name': menu_item.get('ItemName') or line.get('name'),
            'location': menu_item.get('Location') or line.get('location'),
            'price': float(unit),
            'quantity': quantity,
            'subtotal': float(line_total),
        })

    total = (subtotal * (1 + TAX_RATE)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return priced, subtotal, total
