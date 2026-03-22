from os import getenv
import gc
import time
import json
import board
import terminalio
import busio
import adafruit_connection_manager
from adafruit_esp32spi import adafruit_esp32spi
from digitalio import DigitalInOut
import adafruit_requests
import displayio
import rgbmatrix
import framebufferio
import adafruit_display_text.label
from displayio import OnDiskBitmap, TileGrid, Group

# Release any existing displays
displayio.release_displays()

# --- Matrix Properties ---
DISPLAY_WIDTH = 64
DISPLAY_HEIGHT = 32

# 432 Minutes - 7.2 Hours
NETWORK_CALL_INTERVAL = 30
RECONNECT_RETRY_DELAY = 5
TIME_SYNC_INTERVAL = 3600

# --- Icon Properties ---
ICON_WIDTH = 26  # Width of icon assets shown in the scroller.

# --- Text Properties ---
TEXT_START_X = ICON_WIDTH + 4
FONT = terminalio.FONT

# Define color palette
COLOR_PALETTE = { # https://www.color-hex.com/color-palette/71344
    'purple': [213,28,255], # purple
    'blue': [20,110,255], # blue
    'green': [56,234,21], # green
    'yellow': [255,170,0], # yellow
    'red': [255,37,37] # red
}

# Fixed display intensity (used by convert_color / icon palettes). Not adjustable at runtime.
BRIGHTNESS = 2

DEPARTURE_COLOR = COLOR_PALETTE['green']
ARROW_COLOR = COLOR_PALETTE['purple']
ARRIVAL_COLOR = COLOR_PALETTE['green']
FLIGHT_NUM_COLOR = COLOR_PALETTE['green']
DIV_LINE_1_COLOR = COLOR_PALETTE['blue']
DIV_LINE_2_COLOR = COLOR_PALETTE['purple']
AIRCRAFT_COLOR = COLOR_PALETTE['blue']
CLOCK_COLOR = COLOR_PALETTE['green']

del COLOR_PALETTE

def convert_color(col_array):
    """Scale 24-bit RGB based on brightness and return packed color."""
    converted_col = 16**4*int(3*BRIGHTNESS*col_array[0]/10) + 16**2*int(3*BRIGHTNESS*col_array[1]/10) + int(3*BRIGHTNESS*col_array[2]/10)

    return(converted_col)

def icon_bitmap_brightness(palette_data):
    """Apply current brightness scaling to an icon palette in-place."""
    color_len = len(palette_data)
    
    for i in range(color_len):
        current_color = palette_data[i] % 16**6
        r_alt = int(current_color / 16**4)
        g_alt = int((current_color % 16**4) / 16**2)
        b_alt = int(current_color % 16**2)
        
        palette_data[i] = convert_color([r_alt, g_alt, b_alt])

    return(palette_data)

# Initialize the main display group
main_group = Group()

# Initialize the icon group (this remains static on the display)
static_icon_group = Group()

# Lookup tables used to map aircraft code -> readable aircraft name/type.
with open("aircraft_codes.json", 'r') as file:
    aircraft_list = json.load(file)

with open("aircraft_types.json", 'r') as file:
    aircraft_types = json.load(file)

# --- Matrix setup ---
BIT_DEPTH = 2
matrix = rgbmatrix.RGBMatrix(
    width=DISPLAY_WIDTH,
    height=DISPLAY_HEIGHT,
    bit_depth=BIT_DEPTH,
    rgb_pins=[
        board.MTX_R1,
        board.MTX_G1,
        board.MTX_B1,
        board.MTX_R2,
        board.MTX_G2,
        board.MTX_B2,
    ],
    addr_pins=[
        board.MTX_ADDRA,
        board.MTX_ADDRB,
        board.MTX_ADDRC,
        board.MTX_ADDRD,
        #board.MTX_ADDRE,
    ],
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
    tile=1,
    serpentine=True,
    doublebuffer=True,
)

display = framebufferio.FramebufferDisplay(matrix, auto_refresh=True)

alt_width = 0  # Width of the current scrolling aircraft text loop.

# External endpoint that returns a compact time JSON payload.
ada_user = getenv("ADAFRUIT_AIO_USERNAME")
ada_key = getenv("ADAFRUIT_AIO_KEY")
ada_tz = getenv("TIMEZONE")
time_url = "https://io.adafruit.com/api/v2/" + ada_user + "/integrations/time/struct?x-aio-key=" + ada_key + "&tz=" + ada_tz

clock_label = adafruit_display_text.label.Label(FONT, color=convert_color(CLOCK_COLOR), line_spacing = 1.05)
clock_label.anchor_point = (0.5, 0.5)
clock_label.anchored_position = (display.width // 2, 14)
def _format_clock_text(json_response):
    """Build 12-hour clock text with date in a fixed two-line layout."""
    minute_val = f"{json_response['min']:02d}"
    hour_24 = json_response["hour"]
    hour_12 = 12 if hour_24 == 0 else (hour_24 - 12 if hour_24 > 12 else hour_24)

    # Keep width visually stable on the matrix by padding single-digit hour/date.
    time_pad = "" if hour_12 >= 10 else " "
    date_pad = " " if json_response["mon"] < 10 else ""

    return f"{time_pad}{hour_12}:{minute_val}\n{date_pad}{json_response['mon']}/{json_response['mday']}"

def _get_json(url, headers=None):
    """Fetch JSON and always close response to avoid socket/resource leaks."""
    response = None
    try:
        response = requests.get(url, headers=headers)
        return response.json()
    finally:
        if response is not None:
            response.close()

def update_time(*, hours=None, minutes=None, show_colon=True):
    try:
        json_response = _get_json(time_url)

        clock_label.text = _format_clock_text(json_response)
        clock_label.color = convert_color(CLOCK_COLOR)
    except (OSError, ValueError, KeyError) as err:
        print("Error updating current time")

        reconnect_esp()
        
def scroll_text_labels(text_labels):
    """Move scrolling labels left and reset once they exit the viewport."""
    reset_flag = False
    for label in text_labels:
        label.x -= 1

        if hasattr(label, "text"):
            if label.x < -1*alt_width:
                label.x = -1
                reset_flag = True
        else:
            # Keep the matching icon attached to the text loop restart point.
            if reset_flag:
                label.x = alt_width - 27

def _safe_text(value, fallback="N/A"):
    """Return fallback when a string field is missing/empty."""
    return fallback if value == "" else value

def create_static_labels(flight):
    local_text_labels = []

    dep_col_local = convert_color(DEPARTURE_COLOR)
    arv_col_local = convert_color(ARRIVAL_COLOR)
    flight_col_local = convert_color(FLIGHT_NUM_COLOR)

    dep_text = _safe_text(flight.get("origin_code", "N/A"))
    
    text_label = adafruit_display_text.label.Label(
        FONT, color=dep_col_local, x=11, y=5, text=dep_text # moved from x = 20
    )
    local_text_labels.append(text_label)

    arv_text = _safe_text(flight.get("destination_code", "N/A"))
    
    text_label = adafruit_display_text.label.Label(
        FONT, color=arv_col_local, x=36, y=5, text=arv_text # moved from x = 45
    )
    local_text_labels.append(text_label)

    id_text = _safe_text(flight.get("ident", "N/A"))
    
    text_label = adafruit_display_text.label.Label(
        FONT, color=flight_col_local, x=11, y=15, text=id_text # moved from x = 20
    )
    local_text_labels.append(text_label)
        
    return local_text_labels

def create_scrolling_labels(flight, y_position):
    local_text_labels = []

    # `aircraft_type` (e.g. B738) maps to readable text and icon variant.
    plane_code = flight.get("aircraft_type", "Unknown")

    if plane_code in aircraft_list.keys():
        aircraft = aircraft_list[plane_code]
        aircraft_icon_type = aircraft_types[plane_code]
    else:
        aircraft = "Unknown Aircraft (" + plane_code + ")"
        aircraft_icon_type = "j"

    single_line_text = aircraft + "     "
    
    plane_col_local = convert_color(AIRCRAFT_COLOR)
    
    text_label = adafruit_display_text.label.Label(
        FONT, color=plane_col_local, x=1, y=y_position, text=single_line_text
    )
    
    global alt_width
    alt_width = text_label.width

    del text_label
    
    single_line_text = single_line_text + aircraft
    
    text_label2 = adafruit_display_text.label.Label(
        FONT, color=plane_col_local, x=10, y=y_position, text=single_line_text
    )
    local_text_labels.append(text_label2)

    # Load airplane icon
    if aircraft_icon_type == "h":
        heli_tilegrid.x = alt_width-16
        local_text_labels.append(heli_tilegrid)
    elif aircraft_icon_type == "tp":
        prop_tilegrid.x = alt_width-16
        local_text_labels.append(prop_tilegrid)
    elif aircraft_icon_type == "p":
        piston_tilegrid.x = alt_width-16
        local_text_labels.append(piston_tilegrid)
    else:
        plane_tilegrid.x = alt_width-16
        local_text_labels.append(plane_tilegrid)

    return local_text_labels

def create_plane_tilegrid():
    icon_path = "/plane_icon2.bmp"
    icon_bitmap = OnDiskBitmap(icon_path)

    alt_palette = icon_bitmap_brightness(icon_bitmap.pixel_shader)
    
    icon_tilegrid = TileGrid(icon_bitmap, pixel_shader=alt_palette, x=0, y=0)
    del icon_bitmap
    return icon_tilegrid
    
def create_heli_tilegrid():
    icon_path = "/heli.bmp"
    icon_bitmap = OnDiskBitmap(icon_path)

    alt_palette = icon_bitmap_brightness(icon_bitmap.pixel_shader)
    
    icon_tilegrid = TileGrid(icon_bitmap, pixel_shader=alt_palette, x=0, y=0)
    del icon_bitmap
    return icon_tilegrid
    
def create_prop_tilegrid():
    icon_path = "/prop_plane.bmp"
    icon_bitmap = OnDiskBitmap(icon_path)

    alt_palette = icon_bitmap_brightness(icon_bitmap.pixel_shader)
    
    icon_tilegrid = TileGrid(icon_bitmap, pixel_shader=alt_palette, x=0, y=0)
    del icon_bitmap
    return icon_tilegrid
    
def create_piston_tilegrid():
    icon_path = "/piston_plane.bmp"
    icon_bitmap = OnDiskBitmap(icon_path)

    alt_palette = icon_bitmap_brightness(icon_bitmap.pixel_shader)
    
    icon_tilegrid = TileGrid(icon_bitmap, pixel_shader=alt_palette, x=0, y=0)
    del icon_bitmap
    return icon_tilegrid

def create_arrow_tilegrid():
    # Tiny arrow icon for origin -> destination row.
    bitmap = displayio.Bitmap(4, 8, 2)
    
    # Create a palette with one color
    arrow_col_local = convert_color(ARROW_COLOR)
    
    local_palette = displayio.Palette(2)
    local_palette[0] = 0x000000
    local_palette[1] = arrow_col_local

    bitmap.fill(1)
    bitmap[1,0] = 0
    bitmap[1,7] = 0
    bitmap[2,0] = 0
    bitmap[2,1] = 0
    bitmap[2,6] = 0
    bitmap[2,7] = 0
    bitmap[3,0] = 0
    bitmap[3,1] = 0
    bitmap[3,2] = 0
    bitmap[3,5] = 0
    bitmap[3,6] = 0
    bitmap[3,7] = 0
    
    tile_grid = displayio.TileGrid(bitmap, pixel_shader=local_palette)
    del bitmap
    return tile_grid

def create_line_tilegrid():
    bitmap = displayio.Bitmap(64, 2, 3)
    
    # Create a palette with one color
    row1_col_local = convert_color(DIV_LINE_1_COLOR)    
    row2_col_local = convert_color(DIV_LINE_2_COLOR)
    
    local_palette = displayio.Palette(3)
    local_palette[0] = 0x000000
    local_palette[1] = row1_col_local
    local_palette[2] = row2_col_local

    bitmap.fill(0)
    for i in range(62):
        bitmap[i+1,0] = 1
        bitmap[i+1,1] = 2
    
    tile_grid = displayio.TileGrid(bitmap, pixel_shader=local_palette)
    del bitmap
    return tile_grid
    
def update_display_with_flight_data(flight, icon_group, display_group):
    """Rebuild the active flight screen and return scrolling label objects."""
    # Clear previous display items
    while len(display_group):
        display_group.pop()

    # Clear previous icon items
    while len(icon_group):
        icon_group.pop()

    gc.collect()
    
    # Create text labels
    static_labels = create_static_labels(flight)

    # Add text labels to the display group first so they are behind icons
    for label in static_labels:
        display_group.append(label)
    
    text_labels = create_scrolling_labels(flight, 27)

    # Add text labels to the display group first so they are behind icons
    for label in text_labels:
        display_group.append(label)

    # Load arrow bitmap
    icon_group.append(arrow_tilegrid)

    # Load divider rows
    divrow_tilegrid.y = 20
    icon_group.append(divrow_tilegrid)
    
    # Add the icon group to the main display group after text labels
    display_group.append(icon_group)
    
    # Show the updated group on the display
    display.root_group = display_group
    display.refresh()

    gc.collect()
    
    return text_labels

def display_no_flights(icon_group, display_group):
    """Show clock-only fallback screen when no flights are in range."""
    # Clear previous display items
    while len(display_group):
        display_group.pop()

    # Clear previous icon items
    while len(icon_group):
        icon_group.pop()
    
    clock_label.color = convert_color(CLOCK_COLOR)
    display_group.append(clock_label)
    divrow_tilegrid.y = 27
    display_group.append(divrow_tilegrid)

    # Update the display with the new group
    display.root_group = display_group
    display.refresh()

def fetch_flight_data():
    """Fetch first flight in bounds and normalize into a named dict."""
    try:
        json_response = _get_json(full_url, headers=headers)
        
        json_response.pop("full_count", None)
        json_response.pop("version", None)
        
        # Return the first flight payload (if any) without allocating a key list.
        first_result = None
        for key in json_response:
            first_result = json_response.get(key)
            break

        if first_result:
            return dict(zip(FLIGHT_KEYS, first_result))
        return False
    except (OSError, ValueError, KeyError) as err:
        print(err)
        
        print(f"Connected: {esp.connected}")
        print(f"Status: {esp.status}")

        reconnect_esp()
        
        print(f"Connected: {esp.connected}")
        print(f"Status: {esp.status}")
        
        return(False)


plane_tilegrid = create_plane_tilegrid()
plane_tilegrid.y = 22

heli_tilegrid = create_heli_tilegrid()
heli_tilegrid.y = 22

prop_tilegrid = create_prop_tilegrid()
prop_tilegrid.y = 22

piston_tilegrid = create_piston_tilegrid()
piston_tilegrid.y = 22

arrow_tilegrid = create_arrow_tilegrid()
arrow_tilegrid.x = 30 # moved from x = 39
arrow_tilegrid.y = 1

divrow_tilegrid = create_line_tilegrid()
divrow_tilegrid.x = 0
divrow_tilegrid.y = 20

# Network settings
ssid = getenv("CIRCUITPY_WIFI_SSID")
password = getenv("CIRCUITPY_WIFI_PASSWORD")
esp32_cs = DigitalInOut(board.ESP_CS)
esp32_ready = DigitalInOut(board.ESP_BUSY)
esp32_reset = DigitalInOut(board.ESP_RESET)
if "SCK1" in dir(board):
    spi = busio.SPI(board.SCK1, board.MOSI1, board.MISO1)
else:
    spi = busio.SPI(board.SCK, board.MOSI, board.MISO)


esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)
pool = adafruit_connection_manager.get_radio_socketpool(esp)
ssl_context = adafruit_connection_manager.get_radio_ssl_context(esp)
requests = adafruit_requests.Session(pool, ssl_context)

def reconnect_esp():
    """Retry Wi-Fi connection until the ESP32 reports connected."""
    while not esp.is_connected:
        try:
            esp.connect_AP(ssid, password)
        except OSError:
            # Small backoff avoids a hot retry loop when AP is unavailable.
            print("Unable to connect to WiFi, retrying in 5 seconds")
            time.sleep(RECONNECT_RETRY_DELAY)
            continue

reconnect_esp()

headers = {
	"Accept": "*/*",
	"Host": "data-cloud.flightradar24.com",
	"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
}

lat_center = float(getenv("LATITUDE_CENTER"))
lon_center = float(getenv("LONGITUDE_CENTER"))
lat_range = float(getenv("LATITUDE_RANGE"))
lon_range = float(getenv("LONGITUDE_RANGE"))

lat_min = str(lat_center-lat_range)
lat_max = str(lat_center+lat_range)
lon_min = str(lon_center-lon_range)
lon_max = str(lon_center+lon_range)

full_url = "https://data-cloud.flightradar24.com/zones/fcgi/feed.js?bounds=" + lat_max + "," + lat_min + "," + lon_min + "," + lon_max
	
FLIGHT_KEYS = (
    'ident_icao', 'latitude', 'longitude', 'heading', 'altitude', 'groundspeed',
    'squawk', 'alias', 'aircraft_type', 'callsign', 'timestamp', 'origin_code', 'destination_code',
    'fa_flight_id', 'actual_on', 'vertspeed', 'ident', 'actual_off', 'airline'
)

flight_info_named = fetch_flight_data()
previous_flight = ""
flight_data_labels = False
last_check = time.monotonic()

if(flight_info_named):
    flight_data_labels = update_display_with_flight_data(
        flight_info_named, static_icon_group, main_group
    )

    previous_flight = flight_info_named.get("ident", "N/A")
    
else:
    update_time()  # Display whatever time is on the board
    display_no_flights(static_icon_group, main_group)

last_network_call_time = time.monotonic()

print("Startup successful!")

while True:
    loop_now = time.monotonic()

    # Scroll flight description and icon loop.
    if flight_data_labels:
        scroll_text_labels(flight_data_labels)

    if loop_now - last_check >= TIME_SYNC_INTERVAL:
        last_check = loop_now
        update_time()
        
    # Refresh the display
    display.refresh(minimum_frames_per_second=0)

    # Check if NETWORK_CALL_INTERVAL seconds have passed
    if (loop_now - last_network_call_time) >= NETWORK_CALL_INTERVAL:
        gc.collect()
        print("Fetching new flight data...")
        flight_info_named = fetch_flight_data()
        
        if flight_info_named:
            if previous_flight != flight_info_named.get("ident", "N/A"):
                # If flight data is found, update the display with it
                flight_data_labels = update_display_with_flight_data(
                    flight_info_named, static_icon_group, main_group
                )

                previous_flight = flight_info_named.get("ident", "N/A")
        else:
            flight_data_labels = False
            
            last_check = loop_now
            update_time()  # Display whatever time is on the board
            # If no flight data is found, display the "Looking for flights..." message
            display_no_flights(static_icon_group, main_group)

        # Reset the last network call time
        gc.collect()
        last_network_call_time = loop_now
        print(gc.mem_free())

    # Small delay prevents maxing CPU while keeping smooth scroll speed.
    time.sleep(0.1)

