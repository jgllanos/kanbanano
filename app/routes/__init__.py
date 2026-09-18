"""The route modules, one per thing the app has: boards, lists, cards, labels,
checklists, plus logging in. main.py includes all of them."""

from app.routes import auth, boards, cards, checklist, labels, lists

ROUTERS = [auth.router, boards.router, lists.router, cards.router, labels.router, checklist.router]
