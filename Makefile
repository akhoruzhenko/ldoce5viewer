PKG := ldoce5viewer
PYTHON := python

build: clean precompile
	pyinstaller ldoce5viewer.spec

build-x86: clean precompile
	pyinstaller ldoce5viewer-x86.spec

build-arm64: clean precompile
	pyinstaller ldoce5viewer-arm64.spec

install: build
	$(PYTHON) ./setup.py install
	cp ./ldoce5viewer.desktop /usr/share/applications/
	cp ./ldoce5viewer/qtgui/resources/ldoce5viewer.svg /usr/share/pixmaps/
	[ -x /usr/bin/update-desktop-database ] && sudo update-desktop-database -q

sdist: precompile
	$(PYTHON) ./setup.py sdist

precompile: qtui qtresource

qtui:
	cd $(PKG)/qtgui/ui/; $(MAKE)

qtresource:
	cd $(PKG)/qtgui/resources/; $(MAKE)

.PHONY: clean clean-build
clean: clean-build
	cd $(PKG)/qtgui/ui/; $(MAKE) clean
	cd $(PKG)/qtgui/resources/; $(MAKE) clean

clean-build:
	rm -rf build
	rm -rf dist
	rm -f MANIFEST

dmg-arm64: build-arm64
	# Create a folder (named dmg) to prepare our DMG in (if it doesn't already exist).
	mkdir -p dist/dmg

	# Copy the app bundle to the dmg folder.
	cp -r "dist/LDOCE5 Viewer.app" dist/dmg

	create-dmg \
	  --volname "LDOCE5 Viewer arm64" \
	  --volicon "./ldoce5viewer/qtgui/resources/ldoce5viewer.icns" \
	  --window-pos 200 120 \
	  --window-size 600 300 \
	  --icon-size 100 \
	  --icon "LDOCE5 Viewer.app" 175 120 \
	  --hide-extension "LDOCE5 Viewer.app" \
	  --app-drop-link 425 120 \
	  "dist/LDOCE5 Viewer arm64.dmg" \
	  "dist/dmg/"

dmg-x86: build-x86
	# Create a folder (named dmg) to prepare our DMG in (if it doesn't already exist).
	mkdir -p dist/dmg

	# Copy the app bundle to the dmg folder.
	cp -r "dist/LDOCE5 Viewer.app" dist/dmg

	create-dmg \
	  --volname "LDOCE5 Viewer x86" \
	  --volicon "./ldoce5viewer/qtgui/resources/ldoce5viewer.icns" \
	  --window-pos 200 120 \
	  --window-size 600 300 \
	  --icon-size 100 \
	  --icon "LDOCE5 Viewer.app" 175 120 \
	  --hide-extension "LDOCE5 Viewer.app" \
	  --app-drop-link 425 120 \
	  "dist/LDOCE5 Viewer x86.dmg" \
	  "dist/dmg/"
