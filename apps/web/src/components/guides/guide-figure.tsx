import Image from 'next/image';

import dashboard from '../../../public/guides/3-05.webp';
import assets from '../../../public/guides/3-06a.webp';
import proposal from '../../../public/guides/3-07.webp';
import closure from '../../../public/guides/3-09.webp';
import rollback from '../../../public/guides/3-10.webp';
import unverified from '../../../public/guides/3-11.webp';
import readOnly from '../../../public/guides/3-16.webp';
import confirm from '../../../public/guides/3-17.webp';
import reject from '../../../public/guides/3-18.webp';
import login from '../../../public/guides/3-28.webp';

const captures = {
  '3-05': dashboard,
  '3-06a': assets,
  '3-07': proposal,
  '3-09': closure,
  '3-10': rollback,
  '3-11': unverified,
  '3-16': readOnly,
  '3-17': confirm,
  '3-18': reject,
  '3-28': login,
};

export function GuideFigure({ id, alt, caption }: { id: keyof typeof captures; alt: string; caption: string }) {
  return (
    <figure className="my-2 flex min-w-0 flex-col gap-2">
      <Image
        src={captures[id]}
        alt={alt}
        unoptimized
        className="border-border h-auto max-w-full self-start rounded-md border"
      />
      <figcaption className="text-muted-foreground text-sm leading-relaxed">{caption}</figcaption>
    </figure>
  );
}
