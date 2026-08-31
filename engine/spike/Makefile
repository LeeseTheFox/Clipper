OBS_PREFIX = /var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files
OBS_INCDIR = $(OBS_PREFIX)/include
OBS_LIBDIR = $(OBS_PREFIX)/lib

CC      = gcc
CFLAGS  = -Wall -Wextra -std=c11 -I$(OBS_INCDIR)
# --allow-shlib-undefined: suppresses link-time errors for transitive deps of
# libavcodec (libvpx.so.11, libtheoraenc.so.2, etc.) that are not on the host
# at the required SONAME.  They are resolved at runtime via lib_shim/.
LDFLAGS = -L$(OBS_LIBDIR) -Wl,-rpath,$(OBS_LIBDIR) \
          -Wl,--allow-shlib-undefined \
          -lobs

TARGET      = libobs_spike
SRC         = libobs_spike.c

VK_TARGET   = vkcapture_spike
VK_SRC      = vkcapture_spike.c

# Symlink to obs-ffmpeg-mux; the FFmpeg muxer plugin locates it via
# os_get_executable_path_ptr which resolves to the directory of our binary.
MUX_LINK = obs-ffmpeg-mux
MUX_SRC  = $(OBS_PREFIX)/bin/obs-ffmpeg-mux

.PHONY: all clean run setup_shim run_vkcapture

all: $(TARGET) $(MUX_LINK)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

# Symlink obs-ffmpeg-mux next to the binary so the plugin can spawn it.
$(MUX_LINK):
	ln -sf $(MUX_SRC) $(MUX_LINK)

# Build the lib_shim/ directory of versioned codec/pulse symlinks.
setup_shim:
	@bash setup_lib_shim.sh

$(VK_TARGET): $(VK_SRC)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

run_vkcapture: $(VK_TARGET) $(MUX_LINK) setup_shim
	@bash run_vkcapture_spike.sh

clean:
	rm -f $(TARGET) $(VK_TARGET) $(MUX_LINK)
	rm -rf lib_shim/

run: all setup_shim
	@bash run_spike.sh
