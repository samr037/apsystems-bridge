# Custom raw-IEEE-802.15.4 firmware for the CC2652P (Sonoff ZBDongle-P).
# Headless GCC build — no CCS, no SysConfig, no XDCtools. See BUILD.md.
#
# Prerequisite: the SDK libraries must already be built once, from the SDK
# root, with `make build-gcc` (or the targeted CMake build documented in
# BUILD.md). That produces driverlib.lib / drivers_cc26x2.a / nortos_cc26x2.a
# under $(SDK)/build/gcc/...
#
# Override SDK / GCC on the command line if the install paths differ:
#   make SDK=/path/to/sdk GCC=/path/to/arm-none-eabi

SDK ?= /opt/aps-firmware/simplelink_cc13xx_cc26xx_sdk_8_30_01_01
GCC ?= /opt/aps-firmware/xpack-arm-none-eabi-gcc-14.2.1-1.1

NAME = firmware

CC = $(GCC)/bin/arm-none-eabi-gcc
OC = $(GCC)/bin/arm-none-eabi-objcopy
SZ = $(GCC)/bin/arm-none-eabi-size

# --- SDK static libraries (built via the SDK CMake build) -------------------
LIB_DRIVERLIB = $(SDK)/build/gcc/source/ti/devices/cc13x2_cc26x2/lib/gcc/m4f/driverlib/driverlib.lib
LIB_DRIVERS   = $(SDK)/build/gcc/source/ti/drivers/lib/gcc/m4f/drivers_cc26x2/drivers_cc26x2.a
LIB_NORTOS    = $(SDK)/build/gcc/kernel/nortos/lib/gcc/m4f/nortos_cc26x2/nortos_cc26x2.a
# RF core multi-mode driver — ships prebuilt for GCC in the SDK source tree.
LIB_RF        = $(SDK)/source/ti/drivers/rf/lib/gcc/m4f/rf_multiMode_cc26x2.a

ARCH = -mcpu=cortex-m4 -march=armv7e-m -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16

CFLAGS = $(ARCH) \
    -std=c11 -Os -g -gstrict-dwarf \
    -ffunction-sections -fdata-sections \
    -Wall -Wno-unused-parameter \
    -DDeviceFamily_CC13X2_CC26X2 \
    -I. \
    -I$(SDK)/source \
    -I$(SDK)/kernel/nortos \
    -I$(SDK)/kernel/nortos/posix \
    -I$(GCC)/arm-none-eabi/include/newlib-nano \
    -I$(GCC)/arm-none-eabi/include

LFLAGS = $(ARCH) \
    -nostartfiles -static \
    -Wl,--gc-sections \
    -Wl,-T,cc13x2_cc26x2_nortos.lds \
    -Wl,-Map,$(NAME).map \
    -Wl,-u,resetISR \
    --specs=nano.specs

# -u resetISR forces the linker to extract the startup object (reset handler +
# vector table) from nortos_cc26x2.a — ENTRY() alone does not pull an archive
# member. The TI libs are wrapped in a group so the single-pass linker
# resolves their circular dependencies.
LIBS = -Wl,--start-group \
    $(LIB_DRIVERS) $(LIB_RF) $(LIB_NORTOS) $(LIB_DRIVERLIB) \
    -lc -lm -lgcc -lnosys \
    -Wl,--end-group

# ccfg.c is app-specific (the Customer Configuration Area) and is linked as
# a plain object — it wraps the SDK ccfg.c with our bootloader-backdoor
# overrides.
SOURCES = main.c radio.c uart_bridge.c ti_radio_config.c ti_drivers_config.c ccfg.c

OBJECTS = $(SOURCES:.c=.o)

all: $(NAME).hex

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

$(NAME).out: $(OBJECTS) cc13x2_cc26x2_nortos.lds
	$(CC) $(LFLAGS) $(OBJECTS) $(LIBS) -o $@
	$(SZ) $@

$(NAME).hex: $(NAME).out
	$(OC) -O ihex $< $@
	@echo "Built $(NAME).hex"

clean:
	rm -f $(OBJECTS) $(NAME).out $(NAME).hex $(NAME).map

.PHONY: all clean
