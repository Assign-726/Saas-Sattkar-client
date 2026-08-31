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
        if v is not None:
            updated[k] = v
        else:
            updated.pop(k, 0)
    return updated.urlencode()
