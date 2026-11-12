"""Shared list pagination for the hotel admin (rendered by hotel_admin/_pager.html)."""
from django.core.paginator import Paginator

PER_PAGE_CHOICES = (10, 25, 50, 100)


def paginate(request, items, per_page=25):
    """Page a queryset or list from ?page= / ?per_page=; out-of-range pages fall back to the last page."""
    try:
        size = int(request.GET.get('per_page') or per_page)
    except ValueError:
        size = per_page
    if size not in PER_PAGE_CHOICES:
        size = per_page
    page = Paginator(items, size).get_page(request.GET.get('page'))
    page.per_page_choices = PER_PAGE_CHOICES
    return page
