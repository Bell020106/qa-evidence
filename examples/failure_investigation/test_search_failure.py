"""Intentional assertion failure against the localhost fixture."""
from selenium.webdriver.common.by import By
EXPECTED="검색 결과: 사과"

def test_search_result(driver,local_site):
    driver.get(local_site)
    driver.find_element(By.ID,'query').send_keys('사과')
    driver.find_element(By.ID,'search').click()
    assert driver.find_element(By.ID,'result').text == EXPECTED
