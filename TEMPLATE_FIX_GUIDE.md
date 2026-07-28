# Template Rendering Fix Guide

## Issue: Blank Page on Dashboard Menu

The dashboard menu page (`http://127.0.0.1:8200/dashboard/menu/`) is showing a blank page instead of rendering the template content.

## Root Cause Analysis

The most likely causes are:

1. **Authentication Issue**: User is not logged in or doesn't have staff permissions
2. **Template Inheritance Problem**: Base template has CSS issues causing content to be hidden
3. **Static Files Issue**: CSS/JS not loading properly
4. **Django Configuration**: Missing settings or middleware issues

## Immediate Fixes Applied

### 1. Authentication Handling
- Modified views to provide better error messages for authentication issues
- Added explicit redirects to login page for unauthenticated users
- Added permission checks with clear error messages

### 2. Template Debugging
- Created test templates to isolate the issue
- Added debug URLs that bypass authentication
- Fixed sidebar positioning in base template

### 3. Diagnostic Tools
- Created `diagnose_issue.py` script to identify the exact problem

## How to Test and Fix

### Step 1: Run the Diagnostic Script
```bash
cd d:\Hotel\restaurant_project
python diagnose_issue.py
```

This will tell you exactly what's wrong.

### Step 2: Test URLs in Order

1. **Test Simple Template** (No Auth Required):
   ```
   http://127.0.0.1:8200/dashboard/test/
   ```
   If this works → Template system is fine

2. **Test Debug Menu** (No Auth Required):
   ```
   http://127.0.0.1:8200/dashboard/debug-menu/
   ```
   If this works → Menu template is fine

3. **Test Main Menu** (Auth Required):
   ```
   http://127.0.0.1:8200/dashboard/menu/
   ```
   If this redirects to login → Authentication issue

### Step 3: Login and Test

1. **Create Admin User** (if needed):
   ```bash
   python manage.py createsuperuser
   ```

2. **Login**:
   ```
   http://127.0.0.1:8200/login/
   ```
   Use your admin credentials

3. **Test Dashboard**:
   ```
   http://127.0.0.1:8200/dashboard/
   ```

## Quick Fixes for Common Issues

### Issue: "Page not loading" / Blank page
**Solution:**
1. Check if you're logged in
2. Verify user has `is_staff=True`
3. Clear browser cache
4. Check browser console for errors

### Issue: "Permission denied"
**Solution:**
```python
# In Django shell or admin
from django.contrib.auth.models import User
user = User.objects.get(username='your_username')
user.is_staff = True
user.save()
```

### Issue: CSS not loading (page looks broken)
**Solution:**
```bash
python manage.py collectstatic
```

### Issue: Template syntax errors
**Solution:**
- Check Django logs for template errors
- Use the test template to isolate issues

## Test Credentials

If you need test credentials, the diagnostic script creates:
- **Username**: `testuser`
- **Password**: `testpass123`
- **Permissions**: Staff + Superuser

## Expected Behavior After Fix

✅ **Working Dashboard Menu Page Should Show:**
- Restaurant admin sidebar
- Menu items table with search
- Add menu item button
- Pagination controls
- Bootstrap styling

✅ **If Authentication Issues:**
- Redirect to login page
- Clear error messages
- Proper permission handling

## File Changes Made

1. **restaurant_admin/views.py**
   - Improved authentication handling
   - Added debug views
   - Better error messages

2. **restaurant_admin/urls.py**
   - Added test and debug URLs

3. **restaurant_admin/templates/restaurant_admin/base.html**
   - Fixed sidebar positioning
   - Improved CSS structure

4. **Created Diagnostic Tools**
   - `diagnose_issue.py` - Comprehensive testing
   - `test.html` - Simple template test
   - Debug views for troubleshooting

## Next Steps

1. Run the diagnostic script
2. Test the URLs in order
3. Login with proper credentials
4. Verify all dashboard pages work
5. Remove debug URLs when everything works

## Cleanup

When everything is working, remove these debug files:
- `diagnose_issue.py`
- `test.html` template
- Debug URLs from `urls.py`
- Debug views from `views.py`
