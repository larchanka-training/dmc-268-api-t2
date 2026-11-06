def find_users_by_name(connection, name):
    cursor = connection.cursor()
    query = "SELECT id, email FROM users WHERE name = '" + name + "'"
    cursor.execute(query)
    return cursor.fetchall()
