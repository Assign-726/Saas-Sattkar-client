from django import template

register = template.Library()

@register.simple_tag
def query_transform(request, **kwargs):
    """
    Updates the current request's query parameters with new kwargs.
    Usage: {% query_transform request page=2 per_page=50 %}
    """
    updated = request.GET.copy()
    for k, v in kwargs.items():
        if v is not None and v != '':
            updated[k] = v
        else:
            updated.pop(k, None)
    return updated.urlencode()


@register.filter
def inr(value):
    """Whole rupees with Indian digit grouping: 1234567 -> 12,34,567."""
    try:
        n = int(round(float(value or 0)))
    except (TypeError, ValueError):
        return value
    sign, s = ('-' if n < 0 else ''), str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ','.join(groups) + ',' + tail
    return sign + s


@register.filter
def get_item(mapping, key):
    """{{ counts|get_item:'placed' }}"""
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def times(value, factor):
    """{{ unit_price|times:quantity }} for decimals."""
    from decimal import Decimal, InvalidOperation
    try:
        return (Decimal(str(value or 0)) * Decimal(str(factor or 0))).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return ''


@register.filter
def inr2(value):
    """Rupees and paise with Indian grouping: 131465.5 -> 1,31,465.50"""
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(str(value or 0)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return value
    whole, paise = f'{abs(d):.2f}'.split('.')
    return ('-' if d < 0 else '') + inr(int(whole)) + '.' + paise


@register.filter
def signed_inr(value):
    """+₹388.00 / −₹7.00 (true minus sign) for differences."""
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return value
    return ('−' if d < 0 else '+') + '₹' + inr2(abs(d))


@register.filter
def pairs(value):
    """"a:Label A,b:Label B" → [('a', 'Label A'), ('b', 'Label B')] for small fixed lists in templates."""
    return [tuple(part.split(':', 1)) for part in str(value).split(',') if ':' in part]


@register.filter
def minutes_label(value):
    """45 → '45m', 130 → '2h 10m', 1500 → '1d 1h'."""
    try:
        m = int(value)
    except (TypeError, ValueError):
        return ''
    if m < 60:
        return f'{m}m'
    if m < 1440:
        return f'{m // 60}h {m % 60}m' if m % 60 else f'{m // 60}h'
    return f'{m // 1440}d {(m % 1440) // 60}h'
