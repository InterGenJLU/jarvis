"""
TTS Text Normalizer

Converts technical text to human-readable speech.
Handles IPs, ports, years, file sizes, timestamps, etc.
"""

import re
from typing import Callable, Dict


class TTSNormalizer:
    """Converts technical text to natural spoken language"""
    
    def __init__(self):
        # Digit words for IPs and digit-by-digit reading
        self.digit_words = {
            "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
            "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
        }
        
        # Number words for spoken numbers
        self.ones = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
        self.teens = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
                     "sixteen", "seventeen", "eighteen", "nineteen"]
        self.tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
        
        # File extension pronunciations (used by paths + filenames normalizers)
        # Extensions NOT listed here fall through to technical_terms normalizer
        # (which handles JSON, HTML, CSS, XML, SQL, PDF, etc.) or are spoken as-is
        self._ext_pronunciations = {
            # Spell out letter-by-letter
            'py': 'P Y',
            'sh': 'S H',
            'js': 'J S',
            'ts': 'T S',
            'jsx': 'J S X',
            'tsx': 'T S X',
            'md': 'M D',
            'rs': 'R S',
            'rb': 'R B',
            'c': 'C',
            'h': 'H',
            'hpp': 'H P P',
            'csv': 'C S V',
            'ini': 'I N I',
            'yml': 'Y M L',
            'env': 'E N V',
            'gz': 'G Z',
            'ps1': 'P S one',
            'ipynb': 'I P Y notebook',
            # Common spoken forms
            'txt': 'text',
            'cpp': 'C plus plus',
            'wav': 'wave',
            'mp3': 'M P three',
            'cfg': 'config',
            'conf': 'config',
            'docx': 'doc X',
            'xlsx': 'X L S X',
            'pptx': 'P P T X',
        }

        # Register normalizations (order matters!)
        self.normalizations: Dict[str, Callable] = {
            "markdown": self.normalize_markdown,  # Strip markdown formatting FIRST
            "pauses": self.normalize_pauses,  # Convert punctuation pauses
            "ips": self.normalize_ips,
            "ports": self.normalize_ports,
            "cpu_gpu_models": self.normalize_cpu_gpu_models,  # Add before years
            "years": self.normalize_years,
            "file_sizes": self.normalize_file_sizes,
            "timestamps": self.normalize_timestamps,
            "urls": self.normalize_urls,
            "paths": self.normalize_paths,
            "filenames": self.normalize_filenames,  # Standalone filenames (after paths)
            "currency": self.normalize_currency,  # $3.8 billion → spoken (before decimals)
            "decimals": self.normalize_decimals,  # 30.1 → thirty point one (before numbers)
            "technical_terms": self.normalize_technical_terms,
            "numbers": self.normalize_numbers,  # General numbers — MUST be last
        }
    
    def normalize(self, text: str) -> str:
        """Apply all normalization rules"""
        if not text:
            return text
        
        result = text
        
        # Apply each normalization
        for name, func in self.normalizations.items():
            try:
                result = func(result)
            except Exception as e:
                # Log but don't break TTS
                print(f"Warning: Normalization '{name}' failed: {e}")
        
        return result
    
    # ========== Markdown / Formatting ==========
    def normalize_markdown(self, text: str) -> str:
        """Strip markdown formatting that TTS would read literally.

        Removes: **bold**, *italic*, `code`, ~~strikethrough~~, ### headings,
        bullet markers (- , * ), numbered list prefixes (1. ), and [links](url).
        """
        # Bold and italic: **text** or __text__ → text
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'__(.+?)__', r'\1', text)
        # Italic: *text* or _text_ (but not mid-word underscores)
        text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', text)
        # Inline code: `text` → text
        text = re.sub(r'`(.+?)`', r'\1', text)
        # Strikethrough: ~~text~~ → text
        text = re.sub(r'~~(.+?)~~', r'\1', text)
        # Headings: ### text → text
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        # Links: [text](url) → text
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        # Bullet markers at start of line: - item or * item → item
        text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)
        # Numbered list: 1. item → item
        text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
        return text

    # ========== Pauses / Punctuation ==========
    def normalize_pauses(self, text: str) -> str:
        """
        Convert punctuation patterns that should produce audible pauses.
        ' - ' (space-hyphen-space) acts as an em-dash pause in speech.
        Kokoro ignores bare hyphens, so replace with '... ' for a natural pause.
        """
        # " - " → pause (em-dash style interjection)
        text = text.replace(" - ", "... ")
        return text

    # ========== IP Addresses ==========
    def normalize_ips(self, text: str) -> str:
        """192.168.1.1 → 'one nine two dot one six eight dot one dot one'"""
        ipv4_pattern = r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
        
        def ip_to_spoken(match):
            ip = match.group(0)
            parts = ip.split(".")
            spoken_parts = []
            for part in parts:
                # Read each digit separately
                spoken_digits = " ".join(self.digit_words[d] for d in part)
                spoken_parts.append(spoken_digits)
            return " dot ".join(spoken_parts)
        
        return re.sub(ipv4_pattern, ip_to_spoken, text)
    
    # ========== CPU/GPU Models ==========
    def normalize_cpu_gpu_models(self, text: str) -> str:
        """
        Normalize CPU and GPU model numbers for natural speech
        
        Examples:
        - Ryzen 9 5900X -> Ryzen 9 fifty-nine hundred X
        - Core i7-6700K -> Core i7 sixty-seven hundred K
        - RTX 4090 -> RTX forty ninety
        """
        # AMD Ryzen pattern: Ryzen X NNNNX
        def ryzen_model(match):
            series = match.group(1)  # 5, 7, 9
            model = match.group(2)   # 5900, 7950, etc.
            suffix = match.group(3) if match.group(3) else ""  # X, XT, etc.
            
            # Convert model number (e.g., 5900 -> fifty-nine hundred)
            if len(model) == 4:
                first_two = int(model[:2])
                last_two = int(model[2:])
                if last_two == 0:
                    spoken = f"{self._two_digit_words(first_two)} hundred"
                else:
                    spoken = f"{self._two_digit_words(first_two)} {self._two_digit_words(last_two)}"
            else:
                spoken = model
            
            result = f"Ryzen {series} {spoken}"
            if suffix:
                result += f" {suffix}"
            return result
        
        text = re.sub(r"Ryzen (\d) (\d{4})([A-Z]+)?", ryzen_model, text)
        
        # Intel Core pattern: Core iX-NNNNX or Core iX NNNNX
        def intel_core(match):
            series = match.group(1)  # i5, i7, i9
            model = match.group(2)   # 6700, 12700, etc.
            suffix = match.group(3) if match.group(3) else ""  # K, KF, etc.
            
            # Convert model number
            if len(model) == 4:
                first_two = int(model[:2])
                last_two = int(model[2:])
                if last_two == 0:
                    spoken = f"{self._two_digit_words(first_two)} hundred"
                else:
                    spoken = f"{self._two_digit_words(first_two)} {self._two_digit_words(last_two)}"
            else:
                spoken = model
            
            result = f"Core {series} {spoken}"
            if suffix:
                result += f" {suffix}"
            return result
        
        text = re.sub(r"Core (i\d)[-\s](\d{4,5})([A-Z]+)?", intel_core, text, flags=re.IGNORECASE)
        
        return text
    
    # ========== Ports ==========
    def normalize_ports(self, text: str) -> str:
        """
        Port numbers in human-friendly groupings:
        SPT=7680 → 'source port seventy-six eighty'
        DPT=443 → 'destination port four forty-three'
        port 7680 → 'port seventy-six eighty'
        """
        # Match SPT/DPT format (firewall logs)
        port_pattern1 = r"\b(?:SPT|DPT)=([0-9]{1,5})\b"
        
        def firewall_port_to_spoken(match):
            full = match.group(0)
            port_num = match.group(1)
            
            # Determine label
            if "SPT" in full:
                label = "source port"
            elif "DPT" in full:
                label = "destination port"
            else:
                label = "port"
            
            spoken = self._port_number_to_words(port_num)
            return f"{label} {spoken}"
        
        text = re.sub(port_pattern1, firewall_port_to_spoken, text)
        
        # Match plain "port XXXX" or "port: XXXX" format
        port_pattern2 = r"\bport[:\s]+(\d{2,5})\b"
        
        def plain_port_to_spoken(match):
            port_num = match.group(1)
            spoken = self._port_number_to_words(port_num)
            return f"port {spoken}"
        
        text = re.sub(port_pattern2, plain_port_to_spoken, text, flags=re.IGNORECASE)
        
        return text
    
    def _port_number_to_words(self, port_str: str) -> str:
        """
        Convert port to natural grouping:
        7680  -> 'seventy-six eighty'
        37330 -> 'thirty-seven three thirty'
        443   -> 'four forty-three'
        """
        if not port_str.isdigit():
            return port_str
        
        n = int(port_str)
        if n < 0 or n > 65535:
            return port_str
        
        s = str(n)
        
        if len(s) <= 2:
            return self._two_digit_words(n)
        
        if len(s) == 3:
            return self._three_digit_words(n)
        
        if len(s) == 4:
            # 7680 -> 76 80
            a = int(s[:2])
            b = int(s[2:])
            return f"{self._two_digit_words(a)} {self._two_digit_words(b)}"
        
        if len(s) == 5:
            # 37330 -> 37 330
            a = int(s[:2])
            b = int(s[2:])
            return f"{self._two_digit_words(a)} {self._three_digit_words(b)}"
        
        return port_str
    
    def _two_digit_words(self, n: int) -> str:
        """0-99 to words"""
        if n < 0 or n > 99:
            return str(n)
        
        if n < 10:
            return self.ones[n]
        elif n < 20:
            return self.teens[n - 10]
        else:
            tens_digit = n // 10
            ones_digit = n % 10
            if ones_digit == 0:
                return self.tens[tens_digit]
            else:
                return f"{self.tens[tens_digit]}-{self.ones[ones_digit]}"
    
    def _three_digit_words(self, n: int) -> str:
        """0-999 to words (port/model style: 443 → 'four forty-three')"""
        if n < 100:
            return self._two_digit_words(n)

        hundreds = n // 100
        remainder = n % 100

        if remainder == 0:
            return f"{self.ones[hundreds]} hundred"
        else:
            return f"{self.ones[hundreds]} {self._two_digit_words(remainder)}"

    def _three_digit_words_full(self, n: int) -> str:
        """0-999 to proper English (268 → 'two hundred sixty-eight')"""
        if n < 100:
            return self._two_digit_words(n)

        hundreds = n // 100
        remainder = n % 100

        if remainder == 0:
            return f"{self.ones[hundreds]} hundred"
        else:
            return f"{self.ones[hundreds]} hundred {self._two_digit_words(remainder)}"
    
    # ========== Years ==========
    def normalize_years(self, text: str) -> str:
        """
        2024 → 'twenty twenty-four'
        1999 → 'nineteen ninety-nine'
        2000 → 'two thousand'
        """
        year_pattern = r"\b(19\d{2}|20\d{2})\b"
        
        def year_to_spoken(match):
            year = match.group(0)
            y = int(year)
            
            # Special case: 2000-2009
            if 2000 <= y <= 2009:
                if y == 2000:
                    return "two thousand"
                else:
                    return f"two thousand {self.ones[y % 10]}"
            
            # Normal case: split into two parts
            first_two = int(year[:2])
            last_two = int(year[2:])
            
            if last_two == 0:
                return f"{self._two_digit_words(first_two)} hundred"
            else:
                return f"{self._two_digit_words(first_two)} {self._two_digit_words(last_two)}"
        
        return re.sub(year_pattern, year_to_spoken, text)
    
    # ========== File Sizes ==========
    def normalize_file_sizes(self, text: str) -> str:
        """
        5.2GB → 'five point two gigabytes'
        512MB → 'five twelve megabytes'
        """
        size_pattern = r"(\d+(?:\.\d+)?)\s?([KMGTP]B?)\b"
        
        unit_map = {
            "K": "kilobytes", "KB": "kilobytes",
            "M": "megabytes", "MB": "megabytes",
            "G": "gigabytes", "GB": "gigabytes",
            "T": "terabytes", "TB": "terabytes",
            "P": "petabytes", "PB": "petabytes",
        }
        
        def size_to_spoken(match):
            number = match.group(1)
            unit = match.group(2).upper()
            
            unit_word = unit_map.get(unit, unit)
            
            # Keep the number as-is for natural speech
            return f"{number} {unit_word}"
        
        return re.sub(size_pattern, size_to_spoken, text, flags=re.IGNORECASE)
    
    # ========== Timestamps ==========
    def normalize_timestamps(self, text: str) -> str:
        """
        14:30 → 'two thirty PM'
        2024-02-06 14:30 → 'February sixth, two thirty PM'
        """
        # Time only (HH:MM), optionally followed by AM/PM (consumed to avoid "AM AM")
        time_pattern = r"\b(\d{1,2}):(\d{2})\s*(AM|PM|am|pm|a\.m\.|p\.m\.)?"

        def time_to_spoken(match):
            hour = int(match.group(1))
            minute = int(match.group(2))
            explicit_period = match.group(3)
            if explicit_period:
                # Respect the AM/PM from the original text
                period = "PM" if explicit_period.upper().startswith("P") else "AM"
                return self._time_to_spoken_with_period(hour, minute, period)
            return self._time_to_spoken(hour, minute)

        return re.sub(time_pattern, time_to_spoken, text)
    
    def _time_to_spoken(self, hour: int, minute: int) -> str:
        """Convert 24-hour time to spoken format"""
        period = "AM" if hour < 12 else "PM"
        return self._time_to_spoken_with_period(hour, minute, period)

    def _time_to_spoken_with_period(self, hour: int, minute: int, period: str) -> str:
        """Convert time to spoken format with explicit AM/PM"""
        display_hour = hour % 12
        if display_hour == 0:
            display_hour = 12

        if minute == 0:
            return f"{self._two_digit_words(display_hour)} {period}"
        else:
            return f"{self._two_digit_words(display_hour)} {self._two_digit_words(minute)} {period}"
    
    # ========== URLs ==========
    def normalize_urls(self, text: str) -> str:
        """
        https://example.com → 'example dot com'
        Don't read protocol or www
        """
        url_pattern = r"https?://(?:www\.)?([a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+)"
        
        def url_to_spoken(match):
            domain = match.group(1)
            return domain.replace(".", " dot ")
        
        return re.sub(url_pattern, url_to_spoken, text)
    
    # ========== File Paths ==========
    def normalize_paths(self, text: str) -> str:
        """
        /home/user/.local/bin → 'slash home slash user slash dot local slash bin'
        Speaks slashes so paths are unambiguous.
        """
        path_pattern = r"/(?:[\w\-\.]+/)*[\w\-\.]+(?:\.\w+)?"

        def path_to_spoken(match):
            path = match.group(0)
            parts = path.split('/')
            spoken_parts = []
            for part in parts:
                if not part:
                    continue
                # Handle dotfiles/hidden dirs: .local → "dot local"
                if part.startswith('.'):
                    part = 'dot ' + part[1:].replace('_', ' ')
                elif '.' in part:
                    # file.txt → "file dot text"
                    name_parts = part.rsplit('.', 1)
                    if len(name_parts) == 2:
                        ext_spoken = self._pronounce_ext(name_parts[1])
                        part = f"{name_parts[0].replace('_', ' ')} dot {ext_spoken}"
                else:
                    part = part.replace('_', ' ')
                spoken_parts.append(part)
            return 'slash ' + ' slash '.join(spoken_parts)

        return re.sub(path_pattern, path_to_spoken, text)

    # ========== Standalone Filenames ==========
    def _pronounce_ext(self, ext: str) -> str:
        """Look up spoken form for a file extension, or return as-is."""
        return self._ext_pronunciations.get(ext.lower(), ext)

    def normalize_filenames(self, text: str) -> str:
        """
        Normalize standalone filenames (not inside paths) for natural speech.
        jarvis_continuous.py → 'jarvis continuous dot P Y'
        test_backup.sh → 'test backup dot S H'
        report.txt → 'report dot text'
        Paths are already handled by normalize_paths above.
        """
        _exts = (
            r'py|sh|js|ts|jsx|tsx|json|yaml|yml|md|txt|csv|log|cfg|conf|ini|toml|'
            r'html|css|xml|sql|rb|go|rs|java|cpp|c|h|hpp|service|rules|'
            r'wav|mp3|ogg|flac|env|lock|bat|ps1|ipynb|pdf|png|jpg|jpeg|gif|zip|tar|gz|'
            r'docx|xlsx|pptx'
        )
        # Filename with known extension, not preceded by / (paths handled above)
        pattern = rf'(?<!/)\b([\w][\w\-]*)\.((?:{_exts}))\b'

        def filename_to_spoken(match):
            name = match.group(1).replace('_', ' ').strip()
            name = re.sub(r'\s+', ' ', name)  # collapse from leading/double underscores
            ext = self._pronounce_ext(match.group(2))
            return f"{name} dot {ext}"

        return re.sub(pattern, filename_to_spoken, text, flags=re.IGNORECASE)

    # ========== Currency ==========
    def normalize_currency(self, text: str) -> str:
        """
        $3.8 billion → 'three point eight billion dollars'
        $250,000 → 'two hundred fifty thousand dollars'
        $99.99 → 'ninety-nine dollars and ninety-nine cents'
        $5 → 'five dollars'
        """
        # Dollar with scale word: $3.8 billion, $1.5 million
        def _dollar_scale(match):
            amount = match.group(1).replace(',', '')
            scale = match.group(2)
            if '.' in amount:
                whole, frac = amount.split('.', 1)
                frac_spoken = ' '.join(self.digit_words.get(d, d) for d in frac)
                spoken = f"{self._number_to_words(int(whole))} point {frac_spoken}"
            else:
                spoken = self._number_to_words(int(amount))
            return f"{spoken} {scale} dollars"
        text = re.sub(
            r'\$(\d+(?:,\d{3})*(?:\.\d+)?)\s*(billion|million|trillion|thousand)',
            _dollar_scale, text, flags=re.IGNORECASE,
        )

        # Dollar with cents: $99.99
        def _dollar_cents(match):
            dollars = int(match.group(1).replace(',', ''))
            cents = int(match.group(2))
            d_word = self._number_to_words(dollars)
            if cents == 0:
                return f"{d_word} dollars"
            return f"{d_word} dollars and {self._number_to_words(cents)} cents"
        text = re.sub(r'\$(\d+(?:,\d{3})*)\.(\d{2})\b', _dollar_cents, text)

        # Plain dollar: $5, $1,000
        def _plain_dollar(match):
            amount = int(match.group(1).replace(',', ''))
            word = self._number_to_words(amount)
            return f"{word} {'dollar' if amount == 1 else 'dollars'}"
        text = re.sub(r'\$(\d+(?:,\d{3})*)\b', _plain_dollar, text)

        return text

    # ========== Decimal Numbers ==========
    def normalize_decimals(self, text: str) -> str:
        """
        30.1 → 'thirty point one'
        33.4 → 'thirty-three point four'
        3.14 → 'three point one four'
        Runs after currency/file_sizes/IPs so those are already consumed.
        """
        # Decimal number not preceded by $ (currency) or followed by unit suffix (file_sizes)
        def _decimal_to_spoken(match):
            whole = int(match.group(1))
            frac = match.group(2)
            frac_spoken = ' '.join(self.digit_words.get(d, d) for d in frac)
            return f"{self._number_to_words(whole)} point {frac_spoken}"
        # Negative lookbehind for $; negative lookahead for file size units
        return re.sub(
            r'(?<!\$)\b(\d+)\.(\d+)\b(?!\.\d)(?!\s*[KMGTP]B?\b)',
            _decimal_to_spoken, text,
        )

    # ========== Technical Terms ==========
    def normalize_technical_terms(self, text: str) -> str:
        """Common technical acronyms and abbreviations"""
        replacements = {
            # Words that should NOT be spelled out (normalize before acronym rules)
            r"\bJARVIS\b": "Jarvis",

            # Spell out letter-by-letter
            r"\bCPU\b": "C P U",
            r"\bGPU\b": "G P U",
            r"\bRAM\b": "ram",
            r"\bAPI\b": "A P I",
            r"\bUSB\b": "U S B",
            r"\bSSH\b": "S S H",
            r"\bHTTP\b": "H T T P",
            r"\bHTTPS\b": "H T T P S",
            r"\bFTP\b": "F T P",
            r"\bDNS\b": "D N S",
            r"\bVPN\b": "V P N",
            r"\bVS\b": "V S",
            r"\bIPv4\b": "I P V 4",
            r"\bIPv6\b": "I P V 6",
            r"\bIP\b": "I P",
            r"\bURL\b": "U R L",
            r"\bHTML\b": "H T M L",
            r"\bCSS\b": "C S S",
            r"\bSQL\b": "S Q L",
            r"\bJSON\b": "J SON",
            r"\bXML\b": "X M L",
            r"\bPDF\b": "P D F",
            
            # Linux commands
            r"\bsystemctl\b": "system control",
            r"\bsystemd\b": "system d",
            r"\bpkill\b": "p kill",

            # Full words
            r"\betc\b": "etcetera",
            r"\be\.g\.\b": "for example",
            r"\bi\.e\.\b": "that is",
        }
        
        # Note: AM and PM are NOT normalized - they should be spoken as-is
        
        result = text
        for pattern, replacement in replacements.items():
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # Spell out IPv6 addresses character by character (e.g. "2600:1700" → "2 6 0 0 colon 1 7 0 0")
        def _spell_ipv6(match):
            addr = match.group(0)
            parts = []
            for ch in addr:
                if ch == ':':
                    parts.append('colon')
                else:
                    parts.append(ch)
            return ' '.join(parts)
        result = re.sub(r'[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){5,7}', _spell_ipv6, result)

        return result
    
    # ========== General Numbers ==========
    def normalize_numbers(self, text: str) -> str:
        """
        Convert remaining numbers to spoken words.
        Handles comma-formatted (17,268) and plain numbers.
        Must run LAST so specialized handlers (IPs, ports, years) go first.
        """
        # First: comma-formatted numbers like 17,268 or 1,234,567
        comma_num_pattern = r"\b(\d{1,3}(?:,\d{3})+)\b"
        def comma_num_to_spoken(match):
            num_str = match.group(1).replace(",", "")
            return self._number_to_words(int(num_str))
        text = re.sub(comma_num_pattern, comma_num_to_spoken, text)

        # Second: plain numbers (4+ digits to avoid clobbering things already handled)
        # Skip numbers that look like they're part of already-processed text
        plain_num_pattern = r"(?<![a-zA-Z\-])\b(\d{4,})\b(?![a-zA-Z\.\-])"
        def plain_num_to_spoken(match):
            return self._number_to_words(int(match.group(1)))
        text = re.sub(plain_num_pattern, plain_num_to_spoken, text)

        # Third: standalone 1-3 digit numbers
        # Skip numbers already part of words (v2, i7), percentages, or unit suffixes
        small_num_pattern = r"(?<![a-zA-Z\-\d\.])(\d{1,3})(?!\d|%|\.\d|[a-zA-Z])"
        def small_num_to_spoken(match):
            return self._number_to_words(int(match.group(1)))
        text = re.sub(small_num_pattern, small_num_to_spoken, text)

        return text

    def _number_to_words(self, n: int) -> str:
        """Convert an integer to spoken English words."""
        if n < 0:
            return f"negative {self._number_to_words(-n)}"
        if n == 0:
            return "zero"

        if n < 100:
            return self._two_digit_words(n)
        if n < 1000:
            return self._three_digit_words_full(n)

        # Colloquial hundreds: 1,100-9,999 where divisible by 100 but not 1000
        # e.g. 1600 → "sixteen hundred", 2500 → "twenty-five hundred"
        if 1100 <= n <= 9999 and n % 100 == 0 and n % 1000 != 0:
            hundreds = n // 100
            return f"{self._two_digit_words(hundreds)} hundred"

        # Thousands
        if n < 1_000_000:
            thousands = n // 1000
            remainder = n % 1000
            result = f"{self._three_digit_words_full(thousands)} thousand"
            if remainder > 0:
                result += f" {self._three_digit_words_full(remainder)}"
            return result

        # Millions
        if n < 1_000_000_000:
            millions = n // 1_000_000
            remainder = n % 1_000_000
            result = f"{self._three_digit_words_full(millions)} million"
            if remainder > 0:
                thousands = remainder // 1000
                rest = remainder % 1000
                if thousands > 0:
                    result += f" {self._three_digit_words_full(thousands)} thousand"
                if rest > 0:
                    result += f" {self._three_digit_words_full(rest)}"
            return result

        # Billions
        if n < 1_000_000_000_000:
            billions = n // 1_000_000_000
            remainder = n % 1_000_000_000
            result = f"{self._three_digit_words_full(billions)} billion"
            if remainder > 0:
                result += f" {self._number_to_words(remainder)}"
            return result

        # Fallback for very large numbers
        return str(n)

    # ========== Extensibility ==========
    def register_normalization(self, name: str, func: Callable[[str], str]) -> None:
        """Allow skills to register custom normalizations"""
        self.normalizations[name] = func
    
    def unregister_normalization(self, name: str) -> None:
        """Remove a normalization"""
        if name in self.normalizations:
            del self.normalizations[name]


# Global instance
_normalizer_instance = None


def get_normalizer() -> TTSNormalizer:
    """Get or create global normalizer instance"""
    global _normalizer_instance
    if _normalizer_instance is None:
        _normalizer_instance = TTSNormalizer()
    return _normalizer_instance
