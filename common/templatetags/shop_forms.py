"""Accessible bound fields, with server-rendered error and hint associations."""
from django import forms, template

register = template.Library()


@register.filter
def is_check(field):
    return isinstance(field.field.widget, forms.CheckboxInput)


@register.filter
def is_group(field):
    return isinstance(field.field.widget, forms.CheckboxSelectMultiple)


@register.simple_tag
def accessible_input(field):
    attrs = {}
    ids = []
    if field.help_text:
        ids.append(f"{field.auto_id}_helptext")
    if field.errors:
        attrs["aria-invalid"] = "true"
        ids.append(f"{field.auto_id}_error")
    if ids:
        attrs["aria-describedby"] = " ".join(ids)
    if isinstance(field.field.widget, forms.CheckboxInput):
        attrs["class"] = "field-checkbox"
    elif isinstance(field.field.widget, forms.CheckboxSelectMultiple):
        attrs["class"] = "field-checkgroup"
    else:
        attrs["class"] = "field-input"
    return field.as_widget(attrs=attrs)
