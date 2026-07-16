.PHONY: relay-run relay-plugin relay-doctor test ios-build

relay-run:
	uv run relay/herdr_relay.py

relay-plugin:
	herdr plugin link relay/

relay-doctor:
	./relay/doctor.sh

test:
	./tests/run.sh

ios-build:
	cd herdi-ios && swift build
