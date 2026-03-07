.PHONY: build-helper build-driver install-driver uninstall-driver probe-driver check clean

build-helper:
	swift build

build-driver:
	$(MAKE) -C driver

install-driver: build-driver
	bash scripts/install-driver.sh

uninstall-driver:
	bash scripts/uninstall-driver.sh

probe-driver:
	bash scripts/probe-driver.sh

check: build-helper build-driver
	bash scripts/smoke-test.sh

clean:
	rm -rf .build
	$(MAKE) -C driver clean
