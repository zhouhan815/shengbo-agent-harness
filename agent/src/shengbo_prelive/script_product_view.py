"""Text-only product views for the model executing typed script generation."""


def script_product_view(product):
    body = product.get("body")
    if not isinstance(body, dict) or "image" not in body:
        return product
    return {**product, "body": {key: value for key, value in body.items() if key != "image"}}
