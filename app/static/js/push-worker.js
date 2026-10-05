self.addEventListener("push", event => {
    const data = event.data ? event.data.json() : {};

    event.waitUntil(
        self.registration.showNotification(
            data.title || "Neue Zusatzinformation",
            {
                body: data.body || "Im Auftragschat liegt eine neue Nachricht vor.",
                tag: data.tag,
                data: {
                    url: data.url || "/admin/commissions"
                }
            }
        )
    );
});

self.addEventListener("notificationclick", event => {
    event.notification.close();

    event.waitUntil((async () => {
        let url = new URL(
            event.notification.data?.url || "/admin/commissions",
            self.location.origin
        );

        if (
            url.origin !== self.location.origin
            || !url.pathname.startsWith("/admin/commissions")
        ) {
            url = new URL("/admin/commissions", self.location.origin);
        }

        const windows = await self.clients.matchAll({
            type: "window",
            includeUncontrolled: true
        });
        const target = windows.find(
            client => new URL(client.url).origin === url.origin
        );

        if (target) {
            const opened = await target.navigate(url.href);
            if (opened) await opened.focus();
            else await self.clients.openWindow(url.href);
        } else {
            await self.clients.openWindow(url.href);
        }
    })());
});