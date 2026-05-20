# Smart Basket SK
MVP застосунок списків покупок з орієнтовним пошуком цін у Словаччині.

## Stack
Next.js 15, TypeScript, Tailwind, Prisma, PostgreSQL.

## Запуск
1. `npm install`
2. `cp .env.example .env`
3. `npm run db:up`
4. `npm run prisma:generate`
5. `npm run prisma:migrate -- --name init`
6. `npm run prisma:seed`
7. `npm run dev`

## Price adapters
Архітектура передбачає adapters у `lib/adapters` з єдиним інтерфейсом отримання кандидатів ціни.

## Confidence score
- 95-100: висока точність (exact)
- 75-94: достатня точність
- 50-74: приблизно
- 20-49: низька точність
- 0: unknown

## Statuses
exact / promo / estimated / manual / unknown / outdated.
