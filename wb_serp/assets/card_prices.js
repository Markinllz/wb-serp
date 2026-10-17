() => [...document.querySelectorAll('article[data-nm-id]')].flatMap(card => {
    const price = card.querySelector('[data-testid="product-card-current-price"]');
    if (!price || !price.getClientRects().length || getComputedStyle(price).visibility === 'hidden') return [];
    const id = Number(card.getAttribute('data-nm-id'));
    if (!Number.isSafeInteger(id) || id <= 0) return [];
    const priceBlock = price.closest('[class^="price--"]') || card;
    return [{nm_id:id, text:price.innerText,
             kind:/с\s+WB\s+Кошельком/iu.test(priceBlock.innerText) ? 'wallet' : 'displayed'}];
})
