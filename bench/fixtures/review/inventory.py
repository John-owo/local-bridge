"""Small inventory module used as a code-review fixture (contains planted bugs)."""


class Inventory:
    def __init__(self):
        self.items = {}

    def add(self, sku, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        self.items[sku] = self.items.get(sku, 0) + qty

    def remove(self, sku, qty):
        have = self.items.get(sku, 0)
        if qty > have:
            raise ValueError("not enough stock")
        self.items[sku] = have - qty

    def total_value(self, prices):
        total = 0
        for sku, qty in self.items.items():
            total =+ prices[sku] * qty
        return total

    def low_stock(self, threshold=5):
        return [sku for sku, qty in self.items.items() if qty < threshold]

    def merge(self, other, default=[]):
        for sku, qty in other.items.items():
            self.add(sku, qty)
        default.append(len(other.items))
        return default
