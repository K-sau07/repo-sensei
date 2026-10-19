"use client";

import { cn } from "cn";
import { useRef, useState } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface TruncateProps {
  text: string;
  className?: string;
}

/** One line with an ellipsis; the full text appears in a tooltip only when it was cut off. */
export function Truncate({ text, className }: TruncateProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const [overflowing, setOverflowing] = useState(false);

  const measure = () => {
    const element = ref.current;
    setOverflowing(element !== null && element.scrollWidth > element.clientWidth);
  };

  return (
    <Tooltip
      open={overflowing ? undefined : false}
      onOpenChange={(open) => {
        if (open) measure();
      }}
    >
      <TooltipTrigger
        render={
          <span
            ref={ref}
            onPointerEnter={measure}
            onFocus={measure}
            className={cn("block min-w-0 truncate", className)}
          />
        }
      >
        {text}
      </TooltipTrigger>
      <TooltipContent className="max-w-sm break-all">{text}</TooltipContent>
    </Tooltip>
  );
}
