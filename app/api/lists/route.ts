import { prisma } from '@/lib/db';

export async function POST(req: Request) {
  const body = (await req.json()) as { name: string; userId: string };
  const list = await prisma.shoppingList.create({
    data: { name: body.name, userId: body.userId },
  });
  return Response.json(list);
}
