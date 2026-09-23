from django.urls import path
from hotel.views import PublicBranchAvailabilityView

urlpatterns = [
    path('', PublicBranchAvailabilityView.as_view(), name='public-branch-availability'),
]
