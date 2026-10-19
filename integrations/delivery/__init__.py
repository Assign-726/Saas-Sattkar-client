"""Zomato & Swiggy operations: order intake, accept/reject, kitchen status, menu & stock, settlements.

Layout
  crypto.py     – encryption for partner credentials
  platforms.py  – per-platform metadata and adapters (the only place partner payloads are interpreted)
  service.py    – the order pipeline, staff actions, menu sync and settlement reconciliation
  views.py      – the separate Zomato and Swiggy dashboards in the restaurant admin
"""
