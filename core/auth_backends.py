from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()

class EmailOrUsernameModelBackend(ModelBackend):
    """
    Custom authentication backend that allows users to authenticate
    using either their username or their email address.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)

        if not username or not password:
            return None

        # Try to find user by case-insensitive username or email
        users = User.objects.filter(
            Q(username__iexact=username) | Q(email__iexact=username)
        ).distinct()

        for user in users:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user

        return None
