"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

export default function ServerWorkspacesPage() {
  const router = useRouter();
  const params = useParams();
  const serverId = (params.serverId as string) ?? "";

  useEffect(() => {
    if (!serverId) {
      router.replace("/servers");
      return;
    }
    router.replace(`/server/${serverId}`);
  }, [router, serverId]);
  return null;
}
