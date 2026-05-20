import { prisma } from '@/lib/db';

export async function GET(req: Request) {
  const q = new URL(req.url).searchParams.get('q')?.toLowerCase() ?? '';
  const rows = await prisma.productAlias.findMany({
    where: { normalizedAlias: { contains: q } },
    take: 10,
    include: { product: true },
  });

  return Response.json(
    rows.map((row) => ({
      id: row.product.id,
      name: row.product.canonicalName,
      alias: row.alias,
    })),
  );
}
