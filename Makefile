PREFIX ?= /usr/local

install:
	install -Dm755 foco.py $(DESTDIR)$(PREFIX)/bin/foco
	install -Dm644 99-spotlight.rules $(DESTDIR)/etc/udev/rules.d/99-spotlight.rules
	install -Dm644 foco.desktop $(DESTDIR)/etc/xdg/autostart/foco.desktop
	udevadm control --reload-rules || true
	udevadm trigger --subsystem-match=hidraw || true
	@echo
	@echo "Installed. Add yourself to the 'input' group if you are not already:"
	@echo "    sudo usermod -aG input \$$USER"
	@echo "Then log out and back in."

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/foco
	rm -f $(DESTDIR)/etc/udev/rules.d/99-spotlight.rules
	rm -f $(DESTDIR)/etc/xdg/autostart/foco.desktop

.PHONY: install uninstall
