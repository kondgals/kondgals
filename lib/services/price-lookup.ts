import { PriceStatus } from '@prisma/client';
import { prisma } from '@/lib/db';
export async function lookupPrice(productId:string){
  const offer=await prisma.priceOffer.findFirst({where:{productId},orderBy:[{confidence:'desc'},{lastCheckedAt:'desc'}]});
  if(!offer) return {status:PriceStatus.unknown,confidence:0};
  return offer;
}
