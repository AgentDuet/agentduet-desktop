# Forno Tanjong — menu, prices, delivery

Public. Anyone who messages may be told any of this.

**Forno Tanjong is Stanley's pizzeria** — he owns and runs it alongside his product and
engineering work at B3 Networks. So yes: Stanley sells pizza, and his assistant takes the
orders. (This line lives in `knowledge/`, not `owner.md`, because `owner.md` is behaviour and
is never retrieved — a question *about the owner* is answered from granted documents. Without
it the agent correctly answered "Stanley does not sell pizza": the menu named a pizzeria and no
document connected it to him.)

Written to match the declared `pizza_delivery` capability (11:00–21:00, 30-minute delivery
slots, max 6 pizzas per order). If the capability's bounds change, change these too — the
agent will answer from here and book against the bounds, and a mismatch between them reads
as the agent contradicting itself.

## Pizzas

| Pizza | Medium (10") | Large (13") |
|---|---|---|
| Margherita — San Marzano, fior di latte, basil | $18 | $24 |
| Pepperoni — double pepperoni, chilli honey | $21 | $27 |
| Four Cheese — mozzarella, gorgonzola, taleggio, grana | $22 | $28 |
| Hawaiian — smoked ham, grilled pineapple | $20 | $26 |
| Funghi (v) — mixed mushroom, thyme, truffle oil | $21 | $27 |

**Gluten-free** bases: +$4 on any pizza. **Vegan** cheese: +$3.
Gluten free, vegan and nut free options are all on this line — asked either spelling, the
answer is the same. Nut free by default. We cannot guarantee an allergen-free kitchen.

## Sides and drinks

- Garlic focaccia $9 · Rocket & parmesan salad $11 · Tiramisu $9
- San Pellegrino 500ml $4 · Coke / Coke Zero $3

## Orders and delivery

- **Hours:** we **open at 11:00** and **close at 21:00**. We are open **seven days a week**
  — Monday to Friday, **Saturday and Sunday**, and public holidays too. Last order 20:30.
- **Delivery:** 30 minutes from confirmation, within 5 km of Tanjong Pagar. Free over $40,
  otherwise $5.
- **Maximum 6 pizzas per order.** Larger orders are catering — those need a person, not the
  agent, so they escalate.
- Delivery slots are held one at a time: if a slot is taken, the next free one is offered.

## Payment

Card or PayNow on delivery. No account or deposit needed.

## Not covered here

Catering, corporate accounts, invoicing terms, franchise enquiries. None of these are
documented, so the agent will pass them to a human rather than guess.
