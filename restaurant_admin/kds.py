"""
Kitchen Display System helpers: the date window a KDS screen is looking at and the
kitchen performance summary for that window.

The live board always keeps every open ticket (new / cooking) on screen while the
window reaches today, even if it was fired before the window started, so a ticket
from last night never silently drops off the line. Finished tickets only appear
for the chosen dates.
"""
import datetime
from collections import Counter

from django.utils import timezone

RANGES = ('today', 'yesterday', '7d', '30d', 'custom')
MAX_CUSTOM_DAYS = 366
COMPLETED_LIST_CAP = 120  # finished tickets drawn on the board; the summary still counts all


def resolve_window(params, today=None):
    """Return (range_key, start_date, end_date) from ?range=&from=&to= (inclusive local dates)."""
    today = today or timezone.localdate()
    key = (params.get('range') or 'today').lower()
    if key not in RANGES:
        key = 'today'
    if key == 'yesterday':
        y = today - datetime.timedelta(days=1)
        return key, y, y
    if key == '7d':
        return key, today - datetime.timedelta(days=6), today
    if key == '30d':
        return key, today - datetime.timedelta(days=29), today
    if key == 'custom':
        try:
            start = datetime.date.fromisoformat(params.get('from') or '')
            end = datetime.date.fromisoformat(params.get('to') or params.get('from') or '')
        except ValueError:
            return 'today', today, today
        if end < start:
            start, end = end, start
        end = min(end, today)
        start = min(start, end)
        if (end - start).days >= MAX_CUSTOM_DAYS:
            start = end - datetime.timedelta(days=MAX_CUSTOM_DAYS - 1)
        return key, start, end
    return 'today', today, today


def window_bounds(start, end):
    """Aware datetimes [start 00:00, end+1 00:00) in the project's local timezone."""
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.datetime.combine(start, datetime.time.min), tz)
    hi = timezone.make_aware(datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min), tz)
    return lo, hi


def window_label(key, start, end):
    if key == 'today':
        return 'Today'
    if key == 'yesterday':
        return 'Yesterday'
    if start == end:
        return start.strftime('%d %b %Y')
    if start.year == end.year:
        return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"


def ago(moment, now):
    """Short 'ready 4 min ago' style phrase; falls back to a date for old tickets."""
    if not moment:
        return ''
    secs = max(0, int((now - moment).total_seconds()))
    if secs < 60:
        return 'just now'
    if secs < 3600:
        return f'{secs // 60} min ago'
    if secs < 86400:
        return f'{secs // 3600} h ago'
    return timezone.localtime(moment).strftime('%d %b, %I:%M %p')


def fmt_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 3600:
        return f'{seconds // 60}:{seconds % 60:02d}'
    return f'{seconds // 3600}h {seconds % 3600 // 60:02d}m'


class Summary:
    """Accumulates kitchen performance over every ticket in the window (station-filtered)."""

    def __init__(self):
        self.tickets = 0
        self.completed = 0
        self.prep_total = 0
        self.prep_n = 0
        self.on_time = 0
        self.late_done = 0
        self.late_open = 0
        self.items = Counter()
        self.hours = Counter()

    def add(self, kds_status, elapsed, target, display_items, order_time):
        self.tickets += 1
        self.hours[timezone.localtime(order_time).hour] += 1
        for it in display_items:
            self.items[it['name']] += it['quantity'] or 0
        if kds_status == 'COMPLETED':
            self.completed += 1
            if elapsed > 0:
                self.prep_total += elapsed
                self.prep_n += 1
                if elapsed <= target:
                    self.on_time += 1
                else:
                    self.late_done += 1
        elif kds_status == 'DELAYED':
            self.late_open += 1

    def as_dict(self):
        avg = (self.prep_total / self.prep_n) if self.prep_n else None
        peak = self.hours.most_common(1)
        peak_label = ''
        if peak:
            h = peak[0][0]
            peak_label = f"{datetime.time(h).strftime('%I %p').lstrip('0')}–{datetime.time((h + 1) % 24).strftime('%I %p').lstrip('0')}"
        return {
            'tickets': self.tickets,
            'completed': self.completed,
            'avg_prep_seconds': int(avg) if avg is not None else None,
            'avg_prep_display': fmt_duration(avg),
            'on_time_pct': round(self.on_time * 100 / self.prep_n) if self.prep_n else None,
            'late': self.late_done + self.late_open,
            'top_items': [{'name': n, 'qty': q} for n, q in self.items.most_common(5)],
            'peak_hour': peak_label,
            'peak_hour_tickets': peak[0][1] if peak else 0,
        }
