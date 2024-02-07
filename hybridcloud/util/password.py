import string
import random


SPECIAL_CHARACTERS = "+-_.:<>?"


def generate_password(length=16, special_chars=True, must_contain_all=True):
    choices = string.ascii_letters + string.digits
    if special_chars:
        choices += SPECIAL_CHARACTERS
    # Make sure first characeter is a letter or digit as special chars at the beginning confuse some clients/libs
    first_char = random.choice(string.ascii_letters + string.digits)
    password = first_char + ''.join(random.choice(choices) for i in range(length-1))
    if must_contain_all:
        if not _check_contains(password, special_chars):
            return generate_password(length, special_chars, must_contain_all)
    return password


def _check_contains(password, special_chars):
    groups = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if special_chars:
        groups.append(SPECIAL_CHARACTERS)
    for group in groups:
        contains = False
        for char in group:
            if char in password:
                contains = True
                break
        if not contains:
            return False
    return True
