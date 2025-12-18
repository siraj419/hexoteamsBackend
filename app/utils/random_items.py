from random import choice

colors = [
    "red",
    "blue",
    "green",
    "yellow",
    "purple",
    "orange",
    "brown",
    "gray",
    "black",
    "white",
]

# Business Icons (lucide react icons)
icons = [
    "Building2",
    "Store",
    "School",
    "Hospital",
    "Bank",
    "Office",
    "Hotel",
    "Restaurant",
    "Bar",
]

def random_color():
    return choice(colors)

def random_icon():
    return choice(icons)