"""
    Check if the user has the required role.    
"""

def require_manager(user_role: str):
    allowed_roles = ['admin', 'manager']
    if not user_role or user_role.lower() not in allowed_roles:
        raise PermissionError(
            f"You do not have permission to perform this action. Required roles: {', '.join(allowed_roles)}"
        )

def require_authenticated(user_role: str):
    """
        ensure the user has a valid role to access the system.  
    """
    # Valid roles per databases/schema.sql's Users.role CHECK constraint are
    # 'technician' and 'manager'; 'admin' is kept for forward compatibility
    # with require_manager() above, which already treats it as a valid role.
    if not user_role or user_role.lower() not in {'admin', 'manager', 'technician'}:
        raise PermissionError("You must be authenticated to perform this action.")
